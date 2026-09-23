"""Bounded live-model development smoke test, run with an installed wheel and Python -I.

This is an environment/recall check, not a held-out quality or cost benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict
from urllib.parse import urlsplit

import cci
import httpx
from cci.config import Config
from cci.errors import BudgetExceeded, ProviderError
from cci.index import index
from cci.ingest import ingest
from cci.memory import prepare_context
from cci.models import InputMessage
from cci.provider import MemoizedProvider, Provider, ProviderRequest, ProviderResponse
from cci.retrieve import retrieve
from cci.store import HistoryStore
from dotenv import load_dotenv
from openai import APIError, APIStatusError, AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.chat.completion_create_params import CompletionCreateParamsNonStreaming


class SmokeFailure(RuntimeError):
    """An acceptance check failed; messages are fixed codes, never provider error bodies."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise SmokeFailure(code)


class MissingConfiguration(SmokeFailure):
    def __init__(self, names: list[str]) -> None:
        super().__init__("missing_configuration")
        self.names = names


# These select documented Chat Completions compatibility endpoints, not native vendor APIs.
# Model IDs are supplied by the caller and are never restricted to a model catalogue.
PROVIDER_PRESETS = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", ""),
}


@dataclass(frozen=True, slots=True)
class ModelSettings:
    provider: str
    model: str
    base_url: str
    api_key: str = field(repr=False)
    token_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    response_format: Literal["json_object", "prompt"] = "json_object"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ModelSettings:
        provider = env.get("CCI_PROVIDER", "openai").strip().lower()
        require(bool(provider), "CCI_PROVIDER_must_not_be_empty")
        default_url, key_name = PROVIDER_PRESETS.get(provider, ("", ""))
        base_url = env.get("CCI_BASE_URL", "").strip().rstrip("/") or default_url
        if not base_url:
            raise MissingConfiguration(["CCI_BASE_URL"])
        parsed = urlsplit(base_url)
        require(parsed.scheme in ("http", "https") and bool(parsed.hostname), "invalid_CCI_BASE_URL")
        require(
            not (parsed.username or parsed.password or parsed.query or parsed.fragment),
            "CCI_BASE_URL_must_not_contain_credentials_query_or_fragment",
        )
        model = env.get("CCI_MODEL", "").strip()
        if not model and provider == "openai":
            model = env.get("OPENAI_MODEL", "").strip()  # Preserve the existing saved configuration.
        api_key = env.get("CCI_API_KEY", "").strip()
        # A changed endpoint must not silently receive credentials intended for another service.
        if not api_key and base_url == default_url and key_name:
            api_key = env.get(key_name, "").strip()
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        missing = []
        if not model:
            missing.append("CCI_MODEL")
        if not api_key and not local:
            missing.append("CCI_API_KEY")
        if missing:
            raise MissingConfiguration(missing)
        token_field = env.get("CCI_TOKEN_LIMIT_FIELD", "").strip() or (
            "max_completion_tokens" if provider == "openai" else "max_tokens"
        )
        require(token_field in ("max_tokens", "max_completion_tokens"), "invalid_CCI_TOKEN_LIMIT_FIELD")
        response_format = env.get("CCI_RESPONSE_FORMAT", "").strip() or (
            "prompt" if provider == "anthropic" else "json_object"
        )
        require(response_format in ("json_object", "prompt"), "invalid_CCI_RESPONSE_FORMAT")
        return cls(
            provider,
            model,
            base_url,
            api_key,
            "max_tokens" if token_field == "max_tokens" else "max_completion_tokens",
            "prompt" if response_format == "prompt" else "json_object",
        )

    def public_settings(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "requested_model": self.model,
            "endpoint": self.base_url,
            "protocol": "openai_chat_completions",
            "token_limit_field": self.token_limit_field,
            "response_format": self.response_format,
        }

    def make_client(
        self, *, timeout_s: int = 20, http_client: httpx.AsyncClient | None = None
    ) -> AsyncOpenAI:
        official_openai = self.provider == "openai" and self.base_url == PROVIDER_PRESETS["openai"][0]
        return AsyncOpenAI(
            api_key=self.api_key or "local-no-key",
            base_url=self.base_url,
            # Prevent ambient OpenAI account identifiers being forwarded to other providers.
            organization=None if official_openai else "",
            project=None if official_openai else "",
            max_retries=0,
            timeout=timeout_s,
            http_client=http_client,
        )


class ChatCompletionsProvider:
    """One SDK transport for compatible services; the core package accepts any Provider adapter."""

    def __init__(self, client: AsyncOpenAI, settings: ModelSettings, max_output_tokens: int = 1_024) -> None:
        self.client, self.settings, self.max_output_tokens = client, settings, max_output_tokens

    async def completion(self, request: ProviderRequest, max_output_tokens: int) -> ChatCompletion:
        params: CompletionCreateParamsNonStreaming = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": request.prompt},
                {"role": "user", "content": request.evidence_context},
            ],
        }
        if self.settings.token_limit_field == "max_tokens":
            params["max_tokens"] = max_output_tokens
        else:
            params["max_completion_tokens"] = max_output_tokens
        if self.settings.response_format == "json_object":
            params["response_format"] = {"type": "json_object"}
        return await self.client.chat.completions.create(**params)

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        try:
            response = await self.completion(request, self.max_output_tokens)
        except APIError as exc:
            # The held-out harness reports CciError text; keep provider bodies out of that report.
            raise ProviderError(f"{type(exc).__name__}: model request failed") from exc
        usage = response.usage
        return ProviderResponse(
            text=response.choices[0].message.content or "" if response.choices else "",
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            usage_unknown=usage is None,
        )


class FixtureMessage(TypedDict):
    role: str
    content: str


class Fixture(TypedDict):
    description: str
    query: str
    required_source_seq: int
    messages: list[FixtureMessage]


def read_fixture(path: Path) -> Fixture:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), "invalid_fixture")
    messages = value.get("messages")
    require(isinstance(messages, list) and len(messages) == 4, "fixture_requires_four_messages")
    for message in messages:
        require(isinstance(message, dict), "invalid_fixture_message")
        require(message.get("role") in ("user", "assistant"), "invalid_fixture_role")
        content = message.get("content")
        require(isinstance(content, str) and 0 < len(content) <= 400, "invalid_fixture_content")
    query, seq = value.get("query"), value.get("required_source_seq")
    require(isinstance(query, str) and 0 < len(query) <= 400, "invalid_fixture_query")
    require(type(seq) is int and 1 <= seq <= len(messages), "invalid_fixture_source")
    return {
        "description": str(value.get("description", "")),
        "query": query,
        "required_source_seq": seq,
        "messages": messages,
    }


@dataclass(frozen=True, slots=True)
class Limits:
    max_calls: int = 10
    max_completion_tokens: int = 256
    max_input_chars_per_call: int = 8_000
    call_timeout_s: int = 20
    run_timeout_s: int = 180


@dataclass(slots=True)
class CallRecord:
    operation: str
    input_chars: int
    status: str = "pending"
    input_tokens: int | None = None
    output_tokens: int | None = None
    resolved_model: str | None = None
    finish_reason: str | None = None
    error_type: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    elapsed_s: float = 0


class MeasuredProvider:
    def __init__(
        self, client: AsyncOpenAI, settings: ModelSettings, calls: list[CallRecord], limits: Limits
    ) -> None:
        self.inner = ChatCompletionsProvider(client, settings)
        self.calls, self.limits = calls, limits

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        if len(self.calls) >= self.limits.max_calls:
            raise BudgetExceeded("live_smoke_call_limit")
        input_chars = len(request.prompt) + len(request.evidence_context)
        if input_chars > self.limits.max_input_chars_per_call:
            raise BudgetExceeded("live_smoke_input_limit")
        require(request.operation in ("indexing", "tree_navigation"), "unexpected_operation")
        call = CallRecord(request.operation, input_chars)
        self.calls.append(call)  # Count before dispatch, including failures and cancellations.
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.limits.call_timeout_s):
                response = await self.inner.completion(request, self.limits.max_completion_tokens)
            call.resolved_model = response.model
            if response.usage is not None:
                call.input_tokens = response.usage.prompt_tokens
                call.output_tokens = response.usage.completion_tokens
            require(
                type(call.input_tokens) is int
                and call.input_tokens > 0
                and type(call.output_tokens) is int
                and call.output_tokens > 0,
                "missing_or_invalid_provider_usage",
            )
            require(
                call.output_tokens is not None and call.output_tokens <= self.limits.max_completion_tokens,
                "provider_exceeded_output_limit",
            )
            require(bool(response.choices), "missing_model_choice")
            choice = response.choices[0]
            call.finish_reason = choice.finish_reason
            require(
                choice.finish_reason == "stop" and bool(choice.message.content), "incomplete_model_output"
            )
            content = choice.message.content or ""
            try:
                parsed = json.loads(content)
            except ValueError as exc:
                raise SmokeFailure("invalid_model_json") from exc
            require(isinstance(parsed, dict), "invalid_model_schema")
            if request.operation == "indexing":
                require(
                    all(
                        isinstance(parsed.get(key), str) and parsed[key].strip()
                        for key in ("title", "summary")
                    ),
                    "invalid_summary_schema",
                )
            else:
                ids = parsed.get("node_ids")
                require(
                    isinstance(ids, list) and all(isinstance(nid, str) for nid in ids),
                    "invalid_navigation_schema",
                )
            call.status = "ok"
            return ProviderResponse(content, input_tokens=call.input_tokens, output_tokens=call.output_tokens)
        except (Exception, asyncio.CancelledError) as exc:
            # Preserve accounting, then propagate. The CLI never logs potentially secret error bodies.
            call.status, call.error_type = "error", type(exc).__name__
            if isinstance(exc, APIStatusError):
                call.http_status = exc.status_code
                # Only known codes; arbitrary provider strings can contain credentials or payloads.
                # https://developers.openai.com/api/docs/guides/error-codes
                if exc.code in (
                    "insufficient_quota",
                    "rate_limit_exceeded",
                    "slow_down",
                    "credit_balance_exhausted",
                    "organization_usage_limit_exceeded",
                    "organization_spend_limit_exceeded",
                    "project_spend_limit_exceeded",
                ):
                    call.error_code = exc.code
            raise
        finally:
            call.elapsed_s = round(time.monotonic() - started, 3)


def usage_summary(calls: list[CallRecord]) -> dict[str, object]:
    return {
        "attempts": len(calls),
        "input_tokens_observed": sum(call.input_tokens or 0 for call in calls),
        "output_tokens_observed": sum(call.output_tokens or 0 for call in calls),
        "usage_unknown": any(call.input_tokens is None or call.output_tokens is None for call in calls),
        "by_operation": {
            operation: {
                "attempts": len(selected),
                "input_tokens_observed": sum(call.input_tokens or 0 for call in selected),
                "output_tokens_observed": sum(call.output_tokens or 0 for call in selected),
                "usage_unknown": any(
                    call.input_tokens is None or call.output_tokens is None for call in selected
                ),
            }
            for operation in ("indexing", "tree_navigation")
            if (selected := [call for call in calls if call.operation == operation])
        },
    }


async def exercise_fixture(
    provider: Provider,
    fixture: Fixture,
    database: Path,
    result: dict[str, object],
) -> None:
    config = Config(
        cache_backend="none",
        tree_max_children=2,
        target_chunk_size_scalars=1,
        max_provider_attempts_per_op=1,
        per_provider_concurrency=1,
        provider_attempt_limit_index=6,
        provider_attempt_limit_retrieve=4,
        max_tree_navigation_calls=4,
        max_evidence_text_scalars=4_000,
        provider_call_deadline_s=20,
        request_deadline_index_s=120,
        request_deadline_retrieve_s=60,
    )
    async with await HistoryStore.open(str(database), config=config) as store:
        await ingest(
            store,
            store.history_id,
            [InputMessage(**message) for message in fixture["messages"]],
            "live-smoke-development",
            "initial",
        )
        history_id = store.history_id
        messages = await store.get_messages(1, len(fixture["messages"]))
        message_ids = [message.message_id for message in messages]
        # Check before paying: this paraphrase must not succeed through lexical fallback.
        require(not (await retrieve(store, fixture["query"], mode="lexical")).evidence, "lexical_fixture_hit")
        result["lexical_evidence_count"] = 0
        memoized = MemoizedProvider(provider, store.config)
        indexed = await index(store, memoized)
        result["index"] = asdict(indexed)
        require(
            indexed.status == "complete" and indexed.committed_coverage.end_seq == len(messages),
            "index_incomplete",
        )
        unchanged = await index(store, memoized)
        require(unchanged.provider_usage.current_provider_calls == 0, "unchanged_index_made_calls")
        result["unchanged_index_calls"] = 0

    async with await HistoryStore.open(str(database), config=config) as store:
        originals = await store.get_messages(1, len(fixture["messages"]))
        require(store.history_id == history_id, "history_id_changed")
        require([message.message_id for message in originals] == message_ids, "message_ids_changed")
        require([message.original_payload for message in originals] == fixture["messages"], "source_changed")
        result["reopen_preserved_originals"] = True
        context = await prepare_context(
            store,
            fixture["query"],
            mode="tree",
            provider=MemoizedProvider(provider, store.config),
            recent_messages=0,
            max_messages=1,
            max_chars=1_600,
            excerpt_chars=400,
        )
        if context.retrieval is None:
            raise SmokeFailure("missing_retrieval")
        result["routing"] = asdict(context.retrieval.routing)
        result["retrieval_usage"] = asdict(context.retrieval.usage)
        result["evidence"] = [asdict(item) for item in context.items]
        result["context"] = context.text
        require(
            context.retrieval.routing.actual_mode == "tree"
            and bool(context.retrieval.routing.selected_chunk_ids),
            "tree_navigation_required",
        )
        expected = originals[fixture["required_source_seq"] - 1]
        require(
            any(
                item.seq == expected.seq
                and item.message_id == expected.message_id
                and item.source_pointer == "/content"
                and item.excerpt == expected.original_payload["content"]
                for item in context.items
            ),
            "expected_original_evidence_missing",
        )
        result["expected_original_evidence_retrieved"] = True


def package_provenance(wheel: Path) -> dict[str, object]:
    package_path = Path(cci.__file__ or "").resolve()
    require(
        sys.prefix != sys.base_prefix and package_path.is_relative_to(Path(sys.prefix).resolve()),
        "installed_virtualenv_package_required",
    )
    with zipfile.ZipFile(wheel) as archive:
        files = [name for name in archive.namelist() if name.startswith("cci/") and name.endswith(".py")]
        require(bool(files), "wheel_has_no_cci_sources")
        for name in files:
            require(
                (package_path.parent.parent / name).read_bytes() == archive.read(name),
                "installed_package_differs_from_wheel",
            )
    return {
        "version": importlib.metadata.version("chat-context-index"),
        "import_path": str(package_path),
        "python": platform.python_version(),
        "executable": sys.executable,
        "isolated_python": bool(sys.flags.isolated),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "installed_sources_match_wheel": True,
        "openai_version": importlib.metadata.version("openai"),
    }


async def run_live(
    settings: ModelSettings,
    fixture: Fixture,
    calls: list[CallRecord],
    result: dict[str, object],
) -> None:
    limits = Limits()
    async with asyncio.timeout(limits.run_timeout_s):
        async with settings.make_client(timeout_s=limits.call_timeout_s) as client:
            with tempfile.TemporaryDirectory(prefix="cci-live-smoke-") as directory:
                await exercise_fixture(
                    MeasuredProvider(client, settings, calls, limits),
                    fixture,
                    Path(directory) / "history.db",
                    result,
                )
    require({call.operation for call in calls} == {"indexing", "tree_navigation"}, "missing_live_operations")
    require(all(call.status == "ok" for call in calls), "provider_call_failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, help="Explicit local secret file; no directory search")
    parser.add_argument("--provider", help="Preset or a provider label with CCI_BASE_URL configured")
    parser.add_argument("--model", help="Provider's model ID; overrides CCI_MODEL")
    parser.add_argument("--base-url", help="Chat Completions compatibility endpoint; overrides CCI_BASE_URL")
    parser.add_argument(
        "--check-config", action="store_true", help="Validate setup without making model calls"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    calls: list[CallRecord] = []
    result: dict[str, object] = {}
    report: dict[str, object] = {
        "schema_version": 2,
        "evaluation": "development_fixture_live_smoke",
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limits": asdict(Limits()),
        "sdk_retries": 0,
        "package_attempts_per_operation": 1,
        "status": "NOT_RUN",
        "checks": result,
        "scope": "One synthetic development query; no held-out quality, answer generation, or cost claim.",
    }
    try:
        report["package"] = package_provenance(args.wheel)
        require(bool(sys.flags.isolated), "run_with_python_I")
        fixture = read_fixture(args.fixture)
        report["fixture_sha256"] = hashlib.sha256(args.fixture.read_bytes()).hexdigest()
        if args.env_file is not None:
            require(args.env_file.is_file(), "env_file_missing")
            load_dotenv(args.env_file, override=False)
        selection = dict(os.environ)
        for name, value in (
            ("CCI_PROVIDER", args.provider),
            ("CCI_MODEL", args.model),
            ("CCI_BASE_URL", args.base_url),
        ):
            if value is not None:
                selection[name] = value
        settings = ModelSettings.from_env(selection)
        report.update(settings.public_settings())
        if args.check_config:
            report["reason"] = "configuration_verified_without_model_calls"
        else:
            report["status"] = "FAIL"
            asyncio.run(run_live(settings, fixture, calls, result))
            report["status"] = "PASS"
    except MissingConfiguration as exc:
        report["reason"] = "missing_configuration"
        report["missing_environment_variables"] = exc.names
    except (Exception, KeyboardInterrupt) as exc:
        # This CLI boundary records failed runs without leaking SDK error text or losing known usage.
        report["status"] = "FAIL"
        report["error_type"] = type(exc).__name__
        if isinstance(exc, SmokeFailure):
            report["reason"] = str(exc)
    report["calls"] = [asdict(call) for call in calls]
    report["usage"] = usage_summary(calls)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{report['status']}: {args.out}")
    return 0 if report["status"] == "PASS" else 2 if report["status"] == "NOT_RUN" else 1


if __name__ == "__main__":
    raise SystemExit(main())
