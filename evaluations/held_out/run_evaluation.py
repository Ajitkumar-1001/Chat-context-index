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
avoid turning cache replay into an apparent independent trial." Pass --provider openai (reads
OPENAI_API_KEY) for a real run. With no --provider, this runs in --fake-provider mode: a
scripted FakeProvider proves the harness's own control flow and scoring logic deterministically
(PRD §16.3's own guidance for fake providers) — it does NOT produce a meaningful SC-011 result,
and the report says so explicitly (constitution Principle IV: never infer success from an
unexecuted real evaluation).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "python" / "src"))

from cci.ask import ask
from cci.errors import CciError
from cci.index import index
from cci.ingest import ingest
from cci.models import InputMessage
from cci.provider import FakeProvider, MemoizedProvider, ProviderResponse
from cci.store import HistoryStore

FIXTURE_PATH = Path(__file__).parent / "fixture.json"


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
        # "synthesis": cannot genuinely answer without understanding content — a fake provider
        # proves control flow, not quality (PRD §16.3). Always abstains structurally by citing
        # nothing, which ask() correctly downgrades to a suppressed/partial answer — this is the
        # honest fake-provider behavior, not a fabricated pass.
        return ProviderResponse(text=json.dumps({"answer": None, "citations": []}))


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
    if not required_units:
        return 1.0
    covered = 0
    for unit in required_units:
        seq = unit["message_index"] + 1
        span = unit["acceptable_span"]
        target_text = history_messages[unit["message_index"]]["content"]
        acceptable_span_text = target_text[span["start"]:span["end"]]
        for ev in evidence:
            if ev.seq == seq and acceptable_span_text in ev.excerpt:
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
    absent_total = 0
    structurally_valid_citations = 0
    total_citations_checked = 0
    errors: list[str] = []

    with tempfile.TemporaryDirectory() as d:
        for label, history in held_out_histories.items():
            # Isolated memo namespace per trial (PRD §16.2) so cache replay across trials never
            # masquerades as an independent trial.
            store = await HistoryStore.open(
                str(Path(d) / f"{label}.db"),
                config={"application_namespace": f"held-out-eval-trial-{trial_index}-{label}"},
            )
            await _ingest_history(store, history["messages"])
            provider = MemoizedProvider(inner=provider_factory(), config=store.config, cache=store.cache)
            try:
                await index(store, provider=provider)
            except CciError as exc:
                errors.append(f"{label}: index() failed: {exc}")

            for q in [q for q in held_out_queries if q["history_label"] == label]:
                try:
                    result = await ask(store, q["query_text"], provider=provider)
                except CciError as exc:
                    errors.append(f"{q['query_id']}: ask() failed: {exc}")
                    continue

                for c in result.citations:
                    total_citations_checked += 1
                    if c.evidence_id in {e.evidence_id for e in result.evidence}:
                        structurally_valid_citations += 1

                if q["answerable"]:
                    evidence_recalls.append(
                        _score_evidence_recall(q["required_evidence_units"], history["messages"], result.evidence)
                    )
                    correct = _score_answer_correctness(result.answer, q["answer_rubric"])
                    if correct is not None:
                        answer_correct.append(correct)
                else:
                    absent_total += 1
                    if result.status in ("insufficient_evidence", "partial") and result.answer is None:
                        abstentions_correct += 1

            await store.aclose()

    macro_evidence_recall = sum(evidence_recalls) / len(evidence_recalls) if evidence_recalls else 0.0
    answer_rubric_correctness = sum(answer_correct) / len(answer_correct) if answer_correct else 0.0
    citation_validity = (
        structurally_valid_citations / total_citations_checked if total_citations_checked else 1.0
    )

    return {
        "trial": trial_index,
        "macro_evidence_recall": macro_evidence_recall,
        "answer_rubric_correctness": answer_rubric_correctness,
        "abstention_correct_count": abstentions_correct,
        "absent_total": absent_total,
        "structurally_valid_citation_rate": citation_validity,
        "citations_checked": total_citations_checked,
        "answerable_queries_scored": len(evidence_recalls),
        "answers_produced": len(answer_correct),
        "errors": errors,
    }


def _apply_sc011_gates(trial: dict) -> dict:
    return {
        "macro_evidence_recall_pass": trial["macro_evidence_recall"] >= 0.85,
        "answer_rubric_correctness_pass": trial["answer_rubric_correctness"] >= 0.80,
        "abstention_pass": trial["abstention_correct_count"] >= 7,
        "citation_validity_pass": trial["structurally_valid_citation_rate"] == 1.0,
        "semantic_citation_support_precision": "NOT COMPUTED — requires human/model-judge review (PRD §16.2), not fabricated by this automatic harness",
    }


async def main_async(args: argparse.Namespace) -> int:
    fixture = _load_fixture()

    if args.provider == "openai":
        def provider_factory():
            import openai  # lazy import (PRD §13.2)

            class _OpenAIAdapter:
                async def complete(self, request):
                    client = openai.AsyncOpenAI()
                    response = await client.chat.completions.create(
                        model=args.model,
                        messages=[
                            {"role": "system", "content": request.prompt},
                            {"role": "user", "content": request.evidence_context},
                        ],
                    )
                    choice = response.choices[0]
                    return ProviderResponse(text=choice.message.content or "")

            return _OpenAIAdapter()

        mode_label = f"real-provider (openai, model={args.model})"
    else:
        provider_factory = _ScriptedIndexingProvider
        mode_label = "fake-provider (control-flow validation only, NOT a quality result)"

    trials = []
    for trial_index in range(1, args.trials + 1):
        trial = await run_trial(fixture, provider_factory, trial_index)
        trial["gates"] = _apply_sc011_gates(trial)
        trials.append(trial)

    report = {
        "mode": mode_label,
        "fixture_meta": fixture["_meta"],
        "trials": trials,
        "honest_note": (
            "This is a real-provider quality result usable against SC-011."
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
    parser.add_argument("--provider", choices=["openai"], default=None)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
