"""Offline harness tests: the SDK uses an HTTP stub; these are not live-model results."""

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from cci.errors import BudgetExceeded
from cci.provider import ProviderRequest
from openai import AsyncOpenAI, RateLimitError

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("live_smoke", ROOT / "evaluations/live_smoke.py")
assert SPEC is not None and SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SMOKE
SPEC.loader.exec_module(SMOKE)
REQUEST = ProviderRequest("indexing", "Return JSON", "Synthetic evidence")
SETTINGS = SMOKE.ModelSettings.from_env(
    {
        "CCI_PROVIDER": "openai",
        "CCI_MODEL": "offline-stub-model",
        "CCI_API_KEY": "offline-test-key",
    }
)


def completion(content: str, *, usage: bool = True, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-offline-stub",
            "object": "chat.completion",
            "created": 1,
            "model": "offline-stub-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", "content": content},
                }
            ],
            "usage": {"prompt_tokens": 101, "completion_tokens": 11, "total_tokens": 112} if usage else None,
        },
    )


@pytest.mark.parametrize(
    "provider_name, expected_host",
    [
        ("openai", "api.openai.com"),
        ("gemini", "generativelanguage.googleapis.com"),
        ("anthropic", "api.anthropic.com"),
        ("groq", "api.groq.com"),
        ("openrouter", "openrouter.ai"),
        ("ollama", "localhost"),
        ("custom-lab", "models.example"),
    ],
)
@pytest.mark.parametrize("bad_navigation", [False, True], ids=["original-source", "invalid-branch-id"])
def test_reopen_recall_and_reject_fallback(
    tmp_path: Path,
    bad_navigation: bool,
    provider_name: str,
    expected_host: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    selection = {"CCI_PROVIDER": provider_name, "CCI_MODEL": "offline-stub-model"}
    if provider_name != "ollama":
        selection["CCI_API_KEY"] = "offline-test-key"
    if provider_name == "custom-lab":
        selection["CCI_BASE_URL"] = "https://models.example/v1"
    settings = SMOKE.ModelSettings.from_env(selection)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-forward-this-key")
    monkeypatch.setenv("OPENAI_ORG_ID", "private-openai-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "private-openai-project")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == expected_host
        assert request.url.path == urlsplit(settings.base_url).path + "/chat/completions"
        expected_key = "local-no-key" if provider_name == "ollama" else "offline-test-key"
        assert request.headers["authorization"] == f"Bearer {expected_key}"
        if provider_name != "openai":
            assert "private-openai" not in str(request.headers)
        body = json.loads(request.content)
        assert body["model"] == "offline-stub-model"
        token_field = "max_completion_tokens" if provider_name == "openai" else "max_tokens"
        assert body[token_field] == 256
        if provider_name == "anthropic":
            assert "response_format" not in body
        else:
            assert body["response_format"] == {"type": "json_object"}
        prompt, evidence = [message["content"] for message in body["messages"]]
        blocks = re.findall(
            r"<<<CCI_EVIDENCE id=(\S+) source=\S+\n(.*?)\nCCI_EVIDENCE_END>>>", evidence, re.S
        )
        if "Summarize" in prompt:
            output = {"title": "Fixture summary", "summary": " | ".join(text for _, text in blocks)}
        else:
            output = {
                "node_ids": ["not-an-offered-id"]
                if bad_navigation
                else [nid for nid, text in blocks if "Oslo" in text]
            }
        return completion(json.dumps(output))

    async def run() -> None:
        calls = []
        result = {}
        async with settings.make_client(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            assert client.max_retries == 0
            provider = SMOKE.MeasuredProvider(client, settings, calls, SMOKE.Limits())
            fixture = SMOKE.read_fixture(ROOT / "evaluations/fixtures/live-smoke.json")
            if bad_navigation:
                with pytest.raises(SMOKE.SmokeFailure, match="tree_navigation_required"):
                    await SMOKE.exercise_fixture(provider, fixture, tmp_path / "memory.db", result)
                assert not result["evidence"]
                assert "expected_original_evidence_retrieved" not in result
            else:
                await SMOKE.exercise_fixture(provider, fixture, tmp_path / "memory.db", result)
                assert len(requests) == len(calls) == 8  # Six summaries, two navigation steps.
                assert result["lexical_evidence_count"] == result["unchanged_index_calls"] == 0
                assert result["reopen_preserved_originals"] and result["expected_original_evidence_retrieved"]
                assert result["evidence"][0]["excerpt"] == fixture["messages"][0]["content"]
                usage = SMOKE.usage_summary(calls)
                assert usage["input_tokens_observed"] == 808 and usage["output_tokens_observed"] == 88
                assert usage["by_operation"]["indexing"]["attempts"] == 6
                assert usage["by_operation"]["tree_navigation"]["attempts"] == 2
                assert not usage["usage_unknown"]
                assert result["index"]["provider_usage"]["input_tokens"] == 606
                assert result["retrieval_usage"]["input_tokens"] == 202

    asyncio.run(run())


def test_legacy_openai_configuration_and_secret_redaction() -> None:
    settings = SMOKE.ModelSettings.from_env({"OPENAI_API_KEY": "secret-value", "OPENAI_MODEL": "my-model"})
    assert settings.provider == "openai" and settings.model == "my-model"
    assert settings.base_url == "https://api.openai.com/v1"
    assert "secret-value" not in repr(settings)
    assert "secret-value" not in json.dumps(settings.public_settings())


@pytest.mark.parametrize("provider", ["gemini", "anthropic", "groq", "openrouter"])
def test_provider_key_selection_does_not_reuse_openai_credentials(provider: str) -> None:
    selection = {
        "CCI_PROVIDER": provider,
        "CCI_MODEL": "any-model-id",
        "OPENAI_API_KEY": "wrong-key",
        f"{provider.upper()}_API_KEY": "right-key",
    }
    settings = SMOKE.ModelSettings.from_env(selection)
    assert settings.api_key == "right-key"
    selection.pop(f"{provider.upper()}_API_KEY")
    with pytest.raises(SMOKE.MissingConfiguration) as caught:
        SMOKE.ModelSettings.from_env(selection)
    assert caught.value.names == ["CCI_API_KEY"]


def test_custom_endpoint_requires_explicit_key_and_keeps_arbitrary_model_id() -> None:
    selection = {
        "CCI_BASE_URL": "https://models.example/v1/",
        "OPENAI_API_KEY": "do-not-forward",
        "CCI_MODEL": "vendor/model-version:quantized",
    }
    with pytest.raises(SMOKE.MissingConfiguration):
        SMOKE.ModelSettings.from_env(selection)
    selection.update(CCI_PROVIDER="my-service", CCI_API_KEY="explicit-key")
    settings = SMOKE.ModelSettings.from_env(selection)
    assert settings.model == "vendor/model-version:quantized" and settings.api_key == "explicit-key"
    assert settings.base_url == "https://models.example/v1"


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"CCI_PROVIDER": "custom", "CCI_BASE_URL": ""}, "missing_configuration"),
        ({"CCI_BASE_URL": "https://user:secret@example.com/v1"}, "must_not_contain_credentials"),
        ({"CCI_BASE_URL": "https://example.com/v1?key=secret"}, "must_not_contain_credentials"),
        ({"CCI_BASE_URL": "https://example.com/v1#secret"}, "must_not_contain_credentials"),
        ({"CCI_BASE_URL": "file:///tmp/model"}, "invalid_CCI_BASE_URL"),
        ({"CCI_TOKEN_LIMIT_FIELD": "unbounded"}, "invalid_CCI_TOKEN_LIMIT_FIELD"),
        ({"CCI_RESPONSE_FORMAT": "anything"}, "invalid_CCI_RESPONSE_FORMAT"),
    ],
)
def test_invalid_selection_is_rejected_before_dispatch(override: dict[str, str], reason: str) -> None:
    with pytest.raises(SMOKE.SmokeFailure, match=reason):
        SMOKE.ModelSettings.from_env({"CCI_MODEL": "test-model", "CCI_API_KEY": "key", **override})


def test_request_capabilities_can_be_configured_without_changing_adapter() -> None:
    async def run() -> None:
        settings = SMOKE.ModelSettings.from_env(
            {
                "CCI_PROVIDER": "custom",
                "CCI_BASE_URL": "http://127.0.0.1:8080/v1",
                "CCI_MODEL": "local",
                "CCI_TOKEN_LIMIT_FIELD": "max_completion_tokens",
                "CCI_RESPONSE_FORMAT": "prompt",
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["max_completion_tokens"] == 256
            assert "max_tokens" not in body and "response_format" not in body
            return completion('{"title":"Topic","summary":"Text"}')

        async with settings.make_client(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            provider = SMOKE.MeasuredProvider(client, settings, [], SMOKE.Limits())
            assert (await provider.complete(REQUEST)).input_tokens == 101

    asyncio.run(run())


@pytest.mark.parametrize(
    ("content", "usage", "finish", "reason"),
    [
        ('{"title":"Topic","summary":"Text"}', False, "stop", "missing_or_invalid_provider_usage"),
        ('{"title":"Topic","summary":"Text"}', True, "length", "incomplete_model_output"),
        ("not JSON", True, "stop", "invalid_model_json"),
        ('{"title":"Topic"}', True, "stop", "invalid_summary_schema"),
    ],
)
def test_invalid_responses_fail_but_preserve_observed_usage(
    content: str,
    usage: bool,
    finish: str,
    reason: str,
) -> None:
    async def run() -> None:
        calls = []
        transport = httpx.MockTransport(lambda request: completion(content, usage=usage, finish=finish))
        async with AsyncOpenAI(
            api_key="offline-test-key", max_retries=0, http_client=httpx.AsyncClient(transport=transport)
        ) as client:
            provider = SMOKE.MeasuredProvider(client, SETTINGS, calls, SMOKE.Limits())
            with pytest.raises(SMOKE.SmokeFailure, match=reason):
                await provider.complete(REQUEST)
        assert len(calls) == 1 and calls[0].status == "error"
        summary = SMOKE.usage_summary(calls)
        assert summary["usage_unknown"] == (not usage)
        assert summary["input_tokens_observed"] == (101 if usage else 0)
        assert summary["output_tokens_observed"] == (11 if usage else 0)

    asyncio.run(run())


def test_call_and_input_caps_prevent_network_dispatch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completion('{"title":"Topic","summary":"Text"}')

    async def run() -> None:
        calls = []
        async with AsyncOpenAI(
            api_key="offline-test-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            provider = SMOKE.MeasuredProvider(client, SETTINGS, calls, SMOKE.Limits(max_calls=1))
            with pytest.raises(BudgetExceeded, match="input_limit"):
                await provider.complete(ProviderRequest("indexing", "x" * 8_001, ""))
            assert not calls and not requests
            await provider.complete(REQUEST)
            with pytest.raises(BudgetExceeded, match="call_limit"):
                await provider.complete(REQUEST)
        assert len(calls) == len(requests) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "code",
    [
        "insufficient_quota",
        "rate_limit_exceeded",
        "slow_down",
        "credit_balance_exhausted",
        "organization_usage_limit_exceeded",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "untrusted-provider-text",
    ],
)
def test_rate_limit_is_one_attempt_with_unknown_usage(code: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            429,
            json={
                "error": {
                    "message": "private provider details",
                    "type": "rate_limit",
                    "code": code,
                }
            },
        )

    async def run() -> None:
        calls = []
        async with AsyncOpenAI(
            api_key="offline-test-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            provider = SMOKE.MeasuredProvider(client, SETTINGS, calls, SMOKE.Limits())
            with pytest.raises(RateLimitError):
                await provider.complete(REQUEST)
        assert len(calls) == len(requests) == 1
        assert calls[0].error_type == "RateLimitError"
        assert calls[0].http_status == 429
        assert calls[0].error_code == (None if code == "untrusted-provider-text" else code)
        assert "private provider details" not in repr(calls[0])
        assert SMOKE.usage_summary(calls)["usage_unknown"]

    asyncio.run(run())


def test_cancelled_request_remains_in_usage_accounting() -> None:
    async def run() -> None:
        entered = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled request must not finish")

        calls = []
        async with AsyncOpenAI(
            api_key="offline-test-key",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            provider = SMOKE.MeasuredProvider(client, SETTINGS, calls, SMOKE.Limits())
            task = asyncio.create_task(provider.complete(REQUEST))
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == 1 and calls[0].error_type == "CancelledError"
        assert SMOKE.usage_summary(calls)["usage_unknown"]

    asyncio.run(run())


def test_provider_exceeding_output_limit_fails_with_observed_usage_preserved() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = completion('{"title":"Topic","summary":"Text"}').json()
            body["usage"].update(completion_tokens=300, total_tokens=401)
            return httpx.Response(200, json=body)

        calls = []
        async with SETTINGS.make_client(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            provider = SMOKE.MeasuredProvider(client, SETTINGS, calls, SMOKE.Limits())
            with pytest.raises(SMOKE.SmokeFailure, match="provider_exceeded_output_limit"):
                await provider.complete(REQUEST)
        assert SMOKE.usage_summary(calls)["output_tokens_observed"] == 300
        assert calls[0].status == "error"

    asyncio.run(run())


def test_shared_adapter_reports_provider_error_without_secret_body() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "secret-key-in-provider-message"}})

        async with SETTINGS.make_client(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ) as client:
            provider = SMOKE.ChatCompletionsProvider(client, SETTINGS)
            with pytest.raises(SMOKE.ProviderError) as caught:
                await provider.complete(REQUEST)
            assert "secret-key-in-provider-message" not in str(caught.value)

    asyncio.run(run())
