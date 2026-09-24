"""Held-out evaluation harness (T082; PRD §16.2).

Runs the held-out split of evaluations/held_out/fixture.json against `index()`/`ask()` and
scores it against SC-011's gates:

  - macro evidence recall >= 0.85 (answerable queries)
  - answer-rubric correctness >= 0.80
  - correct abstention on >= 7/8 absent-answer cases
  - 100% structurally valid citations
  - semantic citation-support precision >= 0.90 (NOT computed here — requires human/model-judge
    review per PRD §16.2; this harness reports it as "requires manual review", never a fabricated
    number)

PRD §16.2: "Run real-provider quality evaluation three times with isolated memo namespaces to
avoid turning cache replay into an apparent independent trial." Pass --provider and configure
CCI_MODEL/CCI_API_KEY (or a preset's key variable) for a real run. With no --provider,
this runs in deterministic-provider mode: a
scripted FakeProvider proves the harness's own control flow and scoring logic deterministically
(PRD §16.3's own guidance for fake providers) — it does NOT produce a meaningful SC-011 result,
and the report says so explicitly (constitution Principle IV: never infer success from an
unexecuted real evaluation).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from types import ModuleType

import cci
from cci.ask import ask
from cci.errors import CciError
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import MemoizedProvider, ProviderResponse
from cci.store import HistoryStore

FIXTURE_PATH = Path(__file__).parent / "fixture.json"


def _model_helpers() -> ModuleType:
    """Load the sibling evaluation adapter without changing the installed package's import path."""
    name = "cci_live_evaluation_helpers"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "live_smoke.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("live evaluation adapter is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class _ScriptedIndexingProvider:
    """A fake provider whose indexing summaries are deterministic and content-derived (not
    scripted per-call), so it can actually index 20 varied histories without running out of
    canned responses — proves control flow across the whole fixture, not just one call."""

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(self, request) -> ProviderResponse:
        self.call_count += 1
        if request.operation == "indexing":
            return ProviderResponse(text=json.dumps({"title": "Topic", "summary": "Synthetic summary."}))
        if request.operation == "tree_navigation":
            return ProviderResponse(text=json.dumps({"node_ids": []}))
        # "synthesis": cannot genuinely answer without understanding content — a fake provider
        # proves control flow, not quality (PRD §16.3). Always abstains structurally by citing
        # nothing, which ask() correctly downgrades to a suppressed/partial answer — this is the
        # honest fake-provider behavior, not a fabricated pass.
        return ProviderResponse(text=json.dumps({"answer": None, "citations": []}))


class _MeasuredProvider:
    """Count attempts and observed tokens by operation, including retries and indexing."""

    def __init__(self, inner, usage):
        self.inner, self.usage = inner, usage

    async def complete(self, request):
        stage = self.usage.setdefault(request.operation, {
            "attempts": 0, "input_tokens_observed": 0, "output_tokens_observed": 0,
            "usage_unknown": False,
        })
        stage["attempts"] += 1
        try:
            response = await self.inner.complete(request)
        except BaseException:
            stage["usage_unknown"] = True
            raise
        if response.usage_unknown or response.input_tokens is None or response.output_tokens is None:
            stage["usage_unknown"] = True
        if response.input_tokens is not None:
            stage["input_tokens_observed"] += response.input_tokens
        if response.output_tokens is not None:
            stage["output_tokens_observed"] += response.output_tokens
        return response


def _load_fixture() -> dict:
    if not FIXTURE_PATH.exists():
        raise SystemExit(f"{FIXTURE_PATH} not found — run generate_fixture.py first")
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _to_input_message(raw: dict) -> InputMessage:
    return InputMessage(
        role=raw["role"], content=raw["content"],
        tool_calls=raw.get("tool_calls"), tool_call_id=raw.get("tool_call_id"),
    )


async def _ingest_history(store: HistoryStore, messages: list[dict]) -> None:
    """Ingests messages one at a time, preserving fixture order exactly, so fixture
    `message_index` maps 1:1 to the resulting `seq` (seq = message_index + 1) on a fresh store."""
    for i, raw in enumerate(messages):
        await ingest(
            store, store.history_id, [_to_input_message(raw)],
            source_id=raw["source_id"], idempotency_key=f"m{i}",
        )


def _score_evidence_recall(required_units: list[dict], history_messages: list[dict], evidence: list) -> float:
    """A unit is covered by its acceptable span in the annotated message or in any annotated
    word-for-word copy of it (SC-011 as clarified 2026-09-23; units without copies score as before)."""
    if not required_units:
        return 1.0
    covered = 0
    for unit in required_units:
        seqs = {unit["message_index"] + 1, *(i + 1 for i in unit.get("verbatim_copy_indexes", []))}
        span = unit["acceptable_span"]
        target_text = history_messages[unit["message_index"]]["content"]
        acceptable_span_text = target_text[span["start"]:span["end"]]
        for ev in evidence:
            if ev.seq in seqs and acceptable_span_text in ev.excerpt:
                covered += 1
                break
    return covered / len(required_units)


def _score_answer_correctness(answer: str | None, rubric: dict | None) -> bool | None:
    """Automatic proxy only (substring match) — PRD §16.2's real correctness gate is a labeled
    human/model-judge rubric review; this is a mechanical stand-in a harness can compute without
    one, used to validate control flow, not to claim the real correctness gate was evaluated."""
    if rubric is None or answer is None:
        return None
    if rubric["correct_value"] not in answer:
        return False
    return not any(wrong in answer for wrong in rubric["must_not_cite_values"])


async def run_trial(fixture: dict, provider_factory, trial_index: int) -> dict:
    held_out_histories = {h["history_label"]: h for h in fixture["histories"] if h["split"] == "held_out"}
    held_out_queries = [q for q in fixture["queries"] if q["split"] == "held_out"]

    evidence_recalls: list[float] = []
    answer_correct: list[bool] = []
    abstentions_correct = 0
    absent_total = sum(not q["answerable"] for q in held_out_queries)
    answerable_total = len(held_out_queries) - absent_total
    structurally_valid_citations = 0
    total_citations_checked = 0
    errors: list[str] = []
    usage: dict = {}
    query_results: list[dict] = []

    with tempfile.TemporaryDirectory() as d:
        for label, history in held_out_histories.items():
            # Isolated memo namespace per trial (PRD §16.2) so cache replay across trials never
            # masquerades as an independent trial.
            async with await HistoryStore.open(
                str(Path(d) / f"{label}.db"),
                config={"application_namespace": f"held-out-eval-trial-{trial_index}-{label}"},
            ) as store:
                await _ingest_history(store, history["messages"])
                provider = MemoizedProvider(
                    inner=_MeasuredProvider(provider_factory(), usage),
                    config=store.config, cache=store.cache,
                )
                try:
                    previous_end = 0
                    for _ in range(16):
                        report = await index(store, provider=provider)
                        if report.status == "complete":
                            break
                        end = report.committed_coverage.end_seq or 0
                        if end <= previous_end:
                            errors.append(f"{label}: indexing stopped without completing coverage")
                            break
                        previous_end = end
                    else:
                        errors.append(f"{label}: indexing still partial after 16 bounded batches")
                except CciError as exc:
                    errors.append(f"{label}: index() failed: {exc}")

                for q in [q for q in held_out_queries if q["history_label"] == label]:
                    try:
                        result = await ask(store, q["query_text"], provider=provider)
                    except CciError as exc:
                        errors.append(f"{q['query_id']}: ask() failed: {exc}")
                        query_results.append({"query_id": q["query_id"], "status": "error"})
                        continue

                    query_results.append({
                        "query_id": q["query_id"], "query": q["query_text"], "status": result.status,
                        "answer": result.answer, "citations": [asdict(c) for c in result.citations],
                        "evidence": [asdict(e) for e in result.evidence], "usage": asdict(result.usage),
                    })

                    for c in result.citations:
                        total_citations_checked += 1
                        if c.evidence_id in {e.evidence_id for e in result.evidence}:
                            structurally_valid_citations += 1

                    if q["answerable"]:
                        evidence_recalls.append(
                            _score_evidence_recall(
                                q["required_evidence_units"], history["messages"], result.evidence
                            )
                        )
                        correct = _score_answer_correctness(result.answer, q["answer_rubric"])
                        if correct is not None:
                            answer_correct.append(correct)
                    else:
                        # A suppressed/failed synthesis is not evidence of correct abstention.
                        if result.status == "insufficient_evidence" and result.answer is None:
                            abstentions_correct += 1

    # Missing answers and failed requests remain in the denominator; neither can improve a score.
    macro_evidence_recall = sum(evidence_recalls) / answerable_total if answerable_total else 0.0
    answer_rubric_correctness = sum(answer_correct) / answerable_total if answerable_total else 0.0
    citation_validity = (
        structurally_valid_citations / total_citations_checked if total_citations_checked else None
    )

    return {
        "trial": trial_index,
        "macro_evidence_recall": macro_evidence_recall,
        "answer_correctness_proxy": answer_rubric_correctness,
        "abstention_correct_count": abstentions_correct,
        "absent_total": absent_total,
        "structurally_valid_citation_rate": citation_validity,
        "citations_checked": total_citations_checked,
        "answerable_queries_scored": answerable_total,
        "answers_produced": len(answer_correct),
        "errors": errors,
        "provider_usage_by_operation": usage,
        "query_results": query_results,
    }


def _apply_sc011_gates(trial: dict, *, real_provider: bool = False) -> dict:
    if not real_provider:
        return {"status": "NOT RUN — deterministic provider is not a quality evaluation"}
    return {
        "macro_evidence_recall_pass": trial["macro_evidence_recall"] >= 0.85,
        "answer_rubric_correctness": "NOT RUN — substring proxy requires rubric review",
        "abstention_pass": trial["abstention_correct_count"] >= 7,
        "citation_validity_pass": trial["structurally_valid_citation_rate"] == 1.0,
        "no_execution_errors": not trial["errors"],
        "semantic_citation_support_precision": (
            "NOT COMPUTED — requires human/model-judge review (PRD §16.2)"
        ),
    }


async def main_async(args: argparse.Namespace) -> int:
    fixture = _load_fixture()
    if getattr(args, "comparison_plan", None):
        spec = importlib.util.spec_from_file_location(
            "cci_comparison", Path(__file__).with_name("comparison.py"),
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("comparison evaluator unavailable")
        comparison = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = comparison
        spec.loader.exec_module(comparison)
        return await comparison.run(args, _model_helpers(), fixture, _score_evidence_recall)
    client = None
    provider_settings = {}

    if args.provider:
        helpers = _model_helpers()  # SDK dependencies stay optional for the deterministic harness.
        if args.env_file:
            if not Path(args.env_file).is_file():
                raise ValueError("env_file_missing")
            helpers.load_dotenv(args.env_file, override=False)
        selection = dict(os.environ)
        selection["CCI_PROVIDER"] = args.provider
        if args.model is not None:
            selection["CCI_MODEL"] = args.model
        if args.base_url is not None:
            selection["CCI_BASE_URL"] = args.base_url
        settings = helpers.ModelSettings.from_env(selection)
        provider_settings = settings.public_settings()
        provider_settings["max_output_tokens_per_call"] = 1_024
        client = settings.make_client(timeout_s=30)

        def provider_factory():
            return helpers.ChatCompletionsProvider(client, settings)

        mode_label = f"real-provider ({settings.provider}, model={settings.model})"
    else:
        provider_factory = _ScriptedIndexingProvider
        mode_label = "fake-provider (control-flow validation only, NOT a quality result)"

    trials = []
    try:
        for trial_index in range(1, args.trials + 1):
            trial = await run_trial(fixture, provider_factory, trial_index)
            trial["gates"] = _apply_sc011_gates(trial, real_provider=bool(args.provider))
            trials.append(trial)
    finally:
        if client is not None:
            await client.close()

    report = {
        "mode": mode_label,
        "provider_settings": provider_settings,
        "fixture_meta": fixture["_meta"],
        "trials": trials,
        "runtime_package": cci.__file__,
        "release_gate_status": "INCOMPLETE — real-provider quality gates are not fully verified",
        "honest_note": (
            "Real-provider measurements; correctness is an automatic proxy. Rubric and semantic "
            "citation review are still required. Observed tokens are not a dollar-cost comparison."
            if args.provider
            else (
                "NOT a real quality evaluation — no model provider was configured. This run only "
                "proves the harness's ingest/index/ask control flow and scoring logic execute "
                "correctly end-to-end across all 8 held-out histories and 40 queries "
                "(PRD §16.3: fake providers prove deterministic control flow, real providers "
                "report actual end-to-end effects). SC-011 gates above are NOT RUN in any "
                "meaningful sense in this mode; do not cite them as a quality claim."
            )
        ),
    }

    text = json.dumps(report, indent=2, sort_keys=False)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=None, help="Provider label; omit for deterministic mode")
    parser.add_argument("--model", default=None, help="Provider's model ID; otherwise use CCI_MODEL")
    parser.add_argument("--base-url", default=None, help="Custom Chat Completions endpoint")
    parser.add_argument("--env-file", default=None, help="Explicit local configuration file")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--out", default=None)
    parser.add_argument("--comparison-plan", help="Frozen plan for a budgeted memory/baseline comparison")
    parser.add_argument("--wheel", help="Installed wheel to verify for a comparison")
    parser.add_argument("--check-config", action="store_true", help="Validate comparison without model calls")
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")
    if args.comparison_plan and not (args.provider and args.wheel and args.out):
        parser.error("comparison requires --provider, --wheel, and --out")
    if args.check_config and not args.comparison_plan:
        parser.error("--check-config requires --comparison-plan")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
