# Installed-package live-model smoke test

The [current CI-verified wheel passed a fresh live smoke on September 24, 2026](results/production-readiness-20260924/live-smoke-retry-003-summary.json):
six indexing and two tree-navigation calls reported 1,058 input / 301 output tokens, with
zero errors or unknown usage. It retrieved the exact original evidence after reopening SQLite.
This run used `check_provider_capacity.py` with a fresh frozen allocation capped at 10 calls,
100,000 reserved tokens, and $0.04; estimated usage cost was $0.0010699. It establishes one
development smoke, not sustained account capacity or held-out quality. Earlier runs and the
standalone CLI's setup and limits are retained below.

This small evaluation subsystem checks one Python wheel against a selected model provider's
Chat Completions compatibility endpoint. The provider, model ID, API key, and endpoint are configurable.
The [visible development fixture](fixtures/live-smoke.json) has four synthetic messages and
one paraphrased question. It is independent of the reserved held-out data.

## Current environment

The environment prepared on September 23, 2026 is:

```text
/private/tmp/cont-index-live-eval-20260923/venv/bin/python
```

It contains the non-editable `chat-context-index==0.1.0` release candidate, `openai==2.45.0`,
and `python-dotenv==1.1.1`. The [earlier report](results/live-smoke.json) records the import
path and wheel hash and verifies that every installed Python source file matches that wheel.
The environment is outside the checkout; recreate it if temporary files are removed.

**Live smoke passed** on September 23, 2026 with `gemini-3.5-flash-lite`. The installed wheel indexed
all four messages, made zero calls when indexing unchanged history, and preserved original records across
closing and reopening SQLite. For “Where should the service launch?”, tree navigation retrieved the exact
original “The deployment target is Oslo.” with sequence 1, its message ID, and `/content` pointer.
The query had no lexical evidence, and no recent-message reserve was used.

| Successful-run operation | Calls | Actual input tokens | Actual output tokens |
| --- | ---: | ---: | ---: |
| Indexing | 6 | 641 | 234 |
| Tree navigation | 2 | 406 | 54 |
| Total | 8 | 1,047 | 288 |

Every call returned complete JSON and usage; `usage_unknown=false`. All original limits remained active.
The ignored `.env.live-smoke` now selects the working model. This result validates one Python development
fixture, not held-out quality, native TypeScript live calls, answer generation, or cost savings.

Earlier attempts are preserved separately and are **not included in the successful-run totals**:

- The saved `gemini-2.5-flash-exp` model [returned HTTP 404](results/live-smoke-gemini-model-not-found.json).
  The [model catalogue check](results/model-availability.json) found other Gemini models, but the stable
  `gemini-2.5-flash` name [also returned HTTP 404](results/live-smoke-gemini-2.5-unavailable.json).
  Google documents restricted access to 2.5 models for newer projects in its
  [model guidance](https://ai.google.dev/gemini-api/docs/models).
- The first `gemini-3.5-flash-lite` run [timed out on its second indexing call](results/live-smoke-gemini-timeout.json).
  Its first call reported 76 input and 25 output tokens; the cancelled call's usage is unknown.
  One subsequent run passed without changing timeouts, token caps, retries, or the fixture.
- The prior OpenAI run [failed with HTTP 429 `credit_balance_exhausted`](results/live-smoke-2026-09-23T15-45-33.546917_00-00.json).
  OpenAI identifies that code as exhausted prepaid credit in its
  [error reference](https://developers.openai.com/api/docs/guides/error-codes). The earlier
  [sandbox connection failure](results/live-smoke-sandbox-failure.json),
  [first API rejection](results/live-smoke-rate-limit-first.json), and
  [diagnostic rejection](results/live-smoke-rate-limit-diagnostic.json) remain available.

Failed attempts without returned usage stay unknown. These reports do not establish the total billed
cost of all attempts, and provider error bodies and credentials are not logged.

The latest provider-selection checks use real SQLite and the SDK with an HTTP stub. They exercise
all six presets and a custom endpoint, including original-source recall, credential separation,
token limits, missing usage, and the held-out adapter. Stub token counts are test inputs, not live
usage, and are never written as the live evaluation report. Gemini is the only provider with a passing
live report so far.

## Select a provider and model

The Python and TypeScript packages already accept any implementation of their `Provider` contract.
These evaluation scripts share an adapter for the Chat Completions wire format. The installed OpenAI
SDK sends requests to the selected service using that service's credentials; an OpenAI account is
required only when selecting OpenAI. APIs with a different wire format need a native `Provider` adapter.

Set these values in the ignored `.env.live-smoke` file:

```dotenv
CCI_PROVIDER=gemini
CCI_MODEL=your-model-id
CCI_API_KEY=your-provider-key
```

Model IDs are passed through without a catalogue or fixed default. Choose a text model available
to your account that can return complete JSON and token usage within the smoke test's small limits.

| `CCI_PROVIDER` | Default `CCI_BASE_URL` | Optional provider-specific key variable |
| --- | --- | --- |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` |
| `anthropic` | `https://api.anthropic.com/v1` | `ANTHROPIC_API_KEY` |
| `groq` | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| `openrouter` | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | Not required for loopback |

The compatibility endpoints are documented by [Google](https://ai.google.dev/gemini-api/docs/openai),
[Anthropic](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk),
[Groq](https://console.groq.com/docs/openai), [OpenRouter](https://openrouter.ai/docs/quickstart), and
[Ollama](https://docs.ollama.com/api/openai-compatibility). Anthropic describes its compatibility
layer as an evaluation aid; use its native SDK adapter for production features it does not expose.

For another compatible service, use any provider label with an explicit endpoint and key:

```dotenv
CCI_PROVIDER=my-service
CCI_BASE_URL=https://your-provider.example/v1
CCI_MODEL=vendor/model-id
CCI_API_KEY=your-provider-key
```

`CCI_API_KEY` takes precedence over the selected preset's key variable. A custom endpoint requires
an explicit `CCI_API_KEY` unless it is on loopback; changing the endpoint never forwards a preset's
key implicitly. OpenAI organization/project environment values are not forwarded to other services.
Base URLs must not contain embedded credentials, query parameters, or fragments because the endpoint
is recorded in reports. `OPENAI_BASE_URL` is not consulted; use `CCI_BASE_URL` explicitly.

For local Ollama, set `CCI_PROVIDER=ollama` and `CCI_MODEL` to an already available local model.
The local server must be running. No model downloads or service startup happen automatically.

Existing files with `OPENAI_API_KEY` and `OPENAI_MODEL` still work when `CCI_PROVIDER` is unset
or `openai`. Other providers never inherit `OPENAI_MODEL` or `OPENAI_API_KEY`.
Shell variables override the env file. CLI `--provider`, `--model`, and `--base-url` override both.

Two optional settings accommodate protocol differences without another adapter implementation:

- `CCI_TOKEN_LIMIT_FIELD=max_tokens|max_completion_tokens`: chooses the bounded output parameter.
  OpenAI defaults to `max_completion_tokens`; other presets default to `max_tokens`.
- `CCI_RESPONSE_FORMAT=json_object|prompt`: JSON mode or omission of the API-specific format parameter.
  Anthropic defaults to `prompt` because its compatibility endpoint ignores `response_format`.
  JSON instructions and response/schema validation remain active in both modes.

There is no automatic provider fallback or parameter retry. A provider that returns missing usage,
incomplete JSON, or more output tokens than requested fails the smoke test with observed usage retained.

## Run the prepared environment

From the repository root, copy [the configuration template](live-smoke.env.example) to
`.env.live-smoke` if it does not already exist, then fill in the provider, API key, and model locally.
That exact secret filename is
ignored by Git. Do not paste the key into chat. Use a Chat Completions model that supports
JSON output and the configured token-limit parameter; the runner makes no automatic model choice.
Existing shell variables take precedence over values in the explicitly supplied file.

```bash
cp -n evaluations/live-smoke.env.example .env.live-smoke
```

After configuring the file, this command makes paid requests:

```bash
/private/tmp/cont-index-live-eval-20260923/venv/bin/python -I evaluations/live_smoke.py \
  --wheel dist/release/chat_context_index-0.1.0-py3-none-any.whl \
  --fixture evaluations/fixtures/live-smoke.json \
  --env-file .env.live-smoke \
  --out evaluations/results/live-smoke.json
```

Alternatively, export the variables in the invoking shell and omit `--env-file`. No automatic
search for env files occurs. To validate settings and the installed wheel without making requests,
add `--check-config` and use `--out evaluations/results/model-config.json`. The report stays `NOT_RUN`
and the exit code is 2 because no live acceptance check ran. This does not check credentials with the server.
The report contains the selected provider, endpoint, model, synthetic source evidence, and token
counts, but no API keys, request headers, or provider exception bodies.

## Recreate the environment

Use Python 3.11–3.14 and an unused directory outside the checkout. These example commands
build a new candidate separately from the previously verified release artifacts:

```bash
python3 -m venv /tmp/cont-index-live-eval
/tmp/cont-index-live-eval/bin/python -m pip install build -r evaluations/requirements-live.txt
/tmp/cont-index-live-eval/bin/python -m build packages/python --wheel --outdir dist/live-eval
/tmp/cont-index-live-eval/bin/python -m pip install dist/live-eval/chat_context_index-0.1.0-py3-none-any.whl
/tmp/cont-index-live-eval/bin/python -I evaluations/live_smoke.py \
  --wheel dist/live-eval/chat_context_index-0.1.0-py3-none-any.whl \
  --fixture evaluations/fixtures/live-smoke.json \
  --env-file .env.live-smoke \
  --out evaluations/results/live-smoke.json
```

## Acceptance and limits

A `PASS` requires all of the following:

- Python runs with `-I` and imports the installed wheel from the virtual environment.
- Indexing covers all four messages; indexing unchanged history makes zero additional calls.
- Closing and reopening SQLite preserves history/message IDs and every original payload.
- The paraphrase has no lexical evidence. Tree navigation alone, with no recent-message reserve,
  retrieves the complete original first message, with its original ID, sequence, and `/content` pointer.
- Every dispatched model call succeeds with a complete, valid JSON response and positive input/output
  token counts supplied by the API. Indexing and navigation usage are recorded separately and together.

The runner caps the entire invocation at 10 model calls (normally six summaries and two navigation
steps), 256 completion tokens per call, and 8,000 input characters per call. Calls have a 20-second
deadline and the run has a 180-second deadline. Both SDK retries and package retries are disabled;
memoization is disabled. These bounds are **not a dollar cap**: model pricing determines spending,
and a timed-out request can still incur usage that the client cannot observe. The current runner
reports tokens, not a price estimate or savings claim.

Missing configuration exits with code 2 and writes `NOT_RUN`; failed checks/provider failures exit
with code 1 and write `FAIL`. Success exits with code 0. Failed or cancelled attempts stay in the
call ledger; absent usage remains `null`, with `usage_unknown=true`. Totals with unknown usage
represent only observed tokens, not complete billing totals. Re-running creates a new temporary
history and spends again; retain each report separately when comparing attempts.

This smoke does not generate a final answer, test native TypeScript against a real model, measure
held-out quality, or establish lower total cost. Those remain separate evaluations.

## Offline verification

CI installs the evaluation dependencies and runs the HTTP-stub tests without secrets or paid calls:

```bash
python -m pip install -r evaluations/requirements-live.txt pytest==9.1.1
python -m pytest tests/evaluations -q
```

The tests cover preset/custom endpoint routing, safe credential selection, request capabilities,
shared held-out selection, reopen/recall, invalid branch IDs and lexical fallback, missing usage,
truncated/malformed responses, call/input/output limits, rate limits, and cancellation accounting.
