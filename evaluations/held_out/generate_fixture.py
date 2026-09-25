"""Held-out evaluation fixture generator (T081; PRD §16.1).

Builds a repository-owned, privacy-safe, fully synthetic fixture: 100 labeled queries across 20
synthetic histories (~300-600 messages each — the smaller end of PRD §16.1's "approximately
300-3,000", sufficient to exhibit every required property without inflating fixture size),
split by history: 12 histories/60 queries development, 8 histories/40 queries held out (32
answerable, 8 absent-answer). No real personal conversations — every history is generated from
templated synthetic scenarios (PRD §16.1: "No real personal conversations are published by
default").

Each history exhibits, per PRD §16.1: topic switches, old decisions (later superseded),
tool records, repeated statements (identical text, distinct identity — INV-02), conflicting
statements (a later message contradicts an earlier one; the correct answer follows the latest),
and cross-session references (multiple source_id "sessions" within one history referencing
earlier facts).

Each answerable query records: the required evidence unit(s), an acceptable original-message
span (message index + a codepoint offset range into that message's text — matching the
evidence-recall grading rule: "a retrieved evidence unit counts as covered only when the
returned original excerpt includes at least one complete annotated acceptable span for that
unit"), and an answer rubric (the correct current value plus which superseded/wrong values a
correct answer must NOT rely on).

Deterministic (seed 7, matching the reference-fixture benchmark's own seed for consistency
across this repo's fixtures) — reproducible byte-for-byte on every run.

Usage: python generate_fixture.py [--out fixture.json]
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field, asdict
from pathlib import Path

SEED = 7
NUM_DEV_HISTORIES = 12
NUM_HELD_OUT_HISTORIES = 8
QUERIES_PER_HISTORY = 5
MIN_MESSAGES_PER_HISTORY = 300
MAX_MESSAGES_PER_HISTORY = 600

# Each scenario names a "fact slot" that gets established, then optionally superseded by a
# later conflicting statement, then asked about. Filler conversation is generated around these
# to reach the target message count and to create topic switches.
_SCENARIOS = [
    {
        "slot": "deploy_day",
        "establish": "Let's plan to deploy the release on {v1}.",
        "supersede": "Change of plan — let's actually deploy on {v2} instead, {v1} won't work.",
        "values": ["Tuesday", "Wednesday", "Thursday", "next Monday", "the 3rd"],
        "question": "What day are we deploying the release?",
    },
    {
        "slot": "color_scheme",
        "establish": "I think we should go with a {v1} color scheme for the dashboard.",
        "supersede": "Actually, let's switch the dashboard to a {v2} color scheme — {v1} tested poorly.",
        "values": ["blue", "green", "dark-mode", "high-contrast", "amber"],
        "question": "What color scheme did we settle on for the dashboard?",
    },
    {
        "slot": "budget",
        "establish": "The budget for this quarter's infra spend is ${v1}.",
        "supersede": "Finance revised the number — the infra budget is now ${v2}, not ${v1}.",
        "values": ["12,000", "18,500", "9,000", "22,000", "15,750"],
        "question": "What is the current infra budget for this quarter?",
    },
    {
        "slot": "meeting_time",
        "establish": "Let's set the weekly sync for {v1}.",
        "supersede": "Sorry, {v1} doesn't work for everyone — moving the weekly sync to {v2}.",
        "values": ["10am Monday", "2pm Tuesday", "9am Friday", "11am Wednesday", "3pm Thursday"],
        "question": "When is the weekly sync currently scheduled?",
    },
    {
        "slot": "lead_engineer",
        "establish": "{v1} is going to be the lead engineer on this project.",
        "supersede": "{v1} is rolling off — {v2} is taking over as lead engineer now.",
        "values": ["Priya", "Marcus", "Elena", "Jordan", "Sam"],
        "question": "Who is the current lead engineer on the project?",
    },
    {
        "slot": "database_choice",
        "establish": "We're going with {v1} for the new service's database.",
        "supersede": "We reconsidered — {v2} fits better than {v1} for our access patterns.",
        "values": ["PostgreSQL", "SQLite", "DynamoDB", "MySQL", "CockroachDB"],
        "question": "Which database did we choose for the new service?",
    },
    {
        "slot": "vacation_dates",
        "establish": "I'm planning to take vacation the week of {v1}.",
        "supersede": "My vacation moved — now it's the week of {v2}, not {v1}.",
        "values": ["July 14th", "August 4th", "June 9th", "September 22nd", "May 5th"],
        "question": "What week is the vacation currently planned for?",
    },
    {
        "slot": "api_version",
        "establish": "New integrations should target API version {v1}.",
        "supersede": "API version {v1} is deprecated now — target {v2} instead.",
        "values": ["v2", "v3", "v2.1", "v4-beta", "v3.2"],
        "question": "Which API version should new integrations target?",
    },
]

_FILLER_TOPICS = [
    "the onboarding doc needs a rewrite",
    "someone should update the status page copy",
    "the CI pipeline is slow on Fridays for some reason",
    "we should archive the old marketing site",
    "the support queue backlog is growing",
    "let's revisit the naming convention for feature flags",
    "the design review is overdue for the settings page",
    "customer feedback on the new pricing page has been mixed",
    "the on-call rotation needs one more person",
    "someone flagged a typo in the release notes",
]

_TOOL_NAMES = ["search_docs", "lookup_ticket", "run_query", "check_status"]


@dataclass
class Message:
    seq_in_history: int
    source_id: str
    role: str
    content: object
    tool_calls: object = None
    tool_call_id: str | None = None


@dataclass
class EvidenceUnit:
    unit_id: str
    message_index: int  # index into the history's messages list (0-based)
    acceptable_span: dict  # {"start": int, "end": int} — codepoint offsets into that message's text
    # Other messages with identical text; the same span is acceptable in each (SC-011 clarification).
    verbatim_copy_indexes: list = field(default_factory=list)


@dataclass
class Query:
    query_id: str
    history_label: str
    split: str  # "dev" | "held_out"
    query_text: str
    answerable: bool
    required_evidence_units: list = field(default_factory=list)
    answer_rubric: dict | None = None  # {"correct_value": str, "must_not_cite_values": [str]}


def _filler_message(rng: random.Random, source_id: str) -> Message:
    topic = rng.choice(_FILLER_TOPICS)
    role = rng.choice(["user", "assistant"])
    return Message(seq_in_history=-1, source_id=source_id, role=role, content=topic)


def _tool_pair(rng: random.Random, source_id: str) -> list[Message]:
    call_id = f"call-{rng.randint(1000, 9999)}"
    tool_name = rng.choice(_TOOL_NAMES)
    return [
        Message(
            seq_in_history=-1, source_id=source_id, role="assistant", content=None,
            tool_calls=[{"type": "supported-tool-call", "id": call_id, "name": tool_name}],
        ),
        Message(
            seq_in_history=-1, source_id=source_id, role="tool", content=f"{tool_name} result: ok",
            tool_call_id=call_id,
        ),
    ]


def _build_history(rng: random.Random, label: str, target_len: int) -> tuple[list[Message], dict]:
    """Returns (messages, scenario_state) where scenario_state maps slot -> {
    'establish_index', 'supersede_index' (or None), 'current_value', 'superseded_value' (or None)
    } — the ground truth used to label queries."""
    sessions = [f"session-{i}" for i in range(1, rng.randint(3, 6))]
    # At least QUERIES_PER_HISTORY distinct scenarios so every history (dev or held-out) has
    # enough labeled-fact slots to draw its required number of answerable queries from.
    scenarios = rng.sample(_SCENARIOS, k=min(len(_SCENARIOS), rng.randint(QUERIES_PER_HISTORY, len(_SCENARIOS))))

    messages: list[Message] = []
    state: dict[str, dict] = {}

    def emit(source_id: str, role: str, content) -> int:
        messages.append(Message(seq_in_history=len(messages), source_id=source_id, role=role, content=content))
        return len(messages) - 1

    # Interleave scenario establishment/supersession with filler and tool calls, across
    # sessions, to create topic switches and cross-session references. Each scenario's own
    # "establish" must be processed before its "supersede" — shuffle scenario order, not event
    # order within a scenario, so this invariant holds regardless of interleaving.
    from collections import deque

    scenario_order = list(scenarios)
    rng.shuffle(scenario_order)
    per_scenario_events: list[deque] = []
    for sc in scenario_order:
        evs = deque([("establish", sc)])
        if rng.random() < 0.7:  # most scenarios get superseded (conflicting statements)
            evs.append(("supersede", sc))
        per_scenario_events.append(evs)

    def pop_next_event():
        """Pops the next event from a random non-empty scenario queue, preserving each
        scenario's internal establish-before-supersede order."""
        candidates = [q for q in per_scenario_events if q]
        if not candidates:
            return None
        return rng.choice(candidates).popleft()

    def events_remaining() -> bool:
        return any(q for q in per_scenario_events)

    while len(messages) < target_len or events_remaining():
        source_id = rng.choice(sessions)
        # Occasionally repeat an earlier filler statement verbatim (repeated statement,
        # distinct identity — INV-02: content equality is not identity).
        if messages and rng.random() < 0.08:
            prior_text_messages = [m for m in messages if isinstance(m.content, str)]
            if prior_text_messages:
                repeat = rng.choice(prior_text_messages)
                emit(source_id, repeat.role, repeat.content)
                continue

        if rng.random() < 0.1:
            for tm in _tool_pair(rng, source_id):
                messages.append(Message(seq_in_history=len(messages), **{k: v for k, v in asdict(tm).items() if k != "seq_in_history"}))
            continue

        if events_remaining() and rng.random() < 0.3:
            event = pop_next_event()
            kind, sc = event
            slot = sc["slot"]
            if kind == "establish":
                value = sc["values"][0]
                text = sc["establish"].format(v1=value)
                idx = emit(source_id, "user", text)
                state[slot] = {
                    "establish_index": idx, "supersede_index": None,
                    "current_value": value, "superseded_value": None,
                    "establish_text": text, "value": value,
                }
            else:  # supersede
                prev = state[slot]
                new_value = sc["values"][1]
                text = sc["supersede"].format(v1=prev["current_value"], v2=new_value)
                idx = emit(source_id, "user", text)
                prev["supersede_index"] = idx
                prev["superseded_value"] = prev["current_value"]
                prev["current_value"] = new_value
                prev["supersede_text"] = text
            continue

        emit(source_id, rng.choice(["user", "assistant"]), _filler_message(rng, source_id).content)

    # Every scenario's "establish" event is guaranteed processed by the loop condition above
    # (it only exits once every per-scenario queue is empty), so `state` covers every scenario.
    assert all(sc["slot"] in state for sc in scenarios)

    return messages, state


def _build_queries(rng: random.Random, label: str, split: str, state: dict, num_answerable: int, num_absent: int) -> list[Query]:
    queries: list[Query] = []
    slots = list(state.keys())
    rng.shuffle(slots)

    answerable_slots = slots[:num_answerable]
    for i, slot in enumerate(answerable_slots):
        sc = next(s for s in _SCENARIOS if s["slot"] == slot)
        st = state[slot]
        current_index = st["supersede_index"] if st["supersede_index"] is not None else st["establish_index"]
        current_text = st["supersede_text"] if st["supersede_index"] is not None else st["establish_text"]
        span_start = current_text.index(st["current_value"])
        span_end = span_start + len(st["current_value"])
        queries.append(
            Query(
                query_id=f"{label}-q{i + 1}",
                history_label=label,
                split=split,
                query_text=sc["question"],
                answerable=True,
                required_evidence_units=[
                    asdict(EvidenceUnit(
                        unit_id=f"{label}-{slot}-current",
                        message_index=current_index,
                        acceptable_span={"start": span_start, "end": span_end},
                    ))
                ],
                answer_rubric={
                    "correct_value": st["current_value"],
                    "must_not_cite_values": [st["superseded_value"]] if st["superseded_value"] else [],
                },
            )
        )

    for i in range(num_absent):
        queries.append(
            Query(
                query_id=f"{label}-absent{i + 1}",
                history_label=label,
                split=split,
                query_text="What is our company's registered legal entity name in Luxembourg?",
                answerable=False,
                required_evidence_units=[],
                answer_rubric=None,
            )
        )

    return queries


def build_fixture(seed: int = SEED) -> dict:
    rng = random.Random(seed)

    histories = []
    all_queries: list[Query] = []

    def make_history(label: str, split: str, num_answerable: int, num_absent: int) -> None:
        target_len = rng.randint(MIN_MESSAGES_PER_HISTORY, MAX_MESSAGES_PER_HISTORY)
        messages, state = _build_history(rng, label, target_len)
        histories.append({
            "history_label": label,
            "split": split,
            "message_count": len(messages),
            "messages": [asdict(m) for m in messages],
        })
        queries = _build_queries(rng, label, split, state, num_answerable, num_absent)
        # Mechanical, text-only annotation that consumes no randomness: every word-for-word copy of
        # an annotated message is also acceptable evidence (SC-011 as clarified 2026-09-23).
        for query in queries:
            for unit in query.required_evidence_units:
                target = messages[unit["message_index"]].content
                unit["verbatim_copy_indexes"] = [
                    i for i, m in enumerate(messages)
                    if i != unit["message_index"] and isinstance(m.content, str) and m.content == target
                ]
        all_queries.extend(queries)

    for i in range(NUM_DEV_HISTORIES):
        make_history(f"dev-h{i + 1:02d}", "dev", num_answerable=QUERIES_PER_HISTORY, num_absent=0)

    for i in range(NUM_HELD_OUT_HISTORIES):
        make_history(f"held-h{i + 1:02d}", "held_out", num_answerable=4, num_absent=1)

    dev_queries = [q for q in all_queries if q.split == "dev"]
    held_out_queries = [q for q in all_queries if q.split == "held_out"]
    held_out_answerable = [q for q in held_out_queries if q.answerable]
    held_out_absent = [q for q in held_out_queries if not q.answerable]

    assert len(histories) == 20
    assert len(dev_queries) == 60
    assert len(held_out_queries) == 40
    assert len(held_out_answerable) == 32
    assert len(held_out_absent) == 8

    return {
        "_meta": {
            "source": "PRD §16.1",
            "seed": seed,
            "description": (
                "Fully synthetic, repository-owned held-out evaluation fixture — no real "
                "personal conversations. Generated by evaluations/held_out/generate_fixture.py."
            ),
            "history_count": len(histories),
            "dev_history_count": NUM_DEV_HISTORIES,
            "held_out_history_count": NUM_HELD_OUT_HISTORIES,
            "query_count": len(all_queries),
            "dev_query_count": len(dev_queries),
            "held_out_query_count": len(held_out_queries),
            "held_out_answerable_count": len(held_out_answerable),
            "held_out_absent_count": len(held_out_absent),
        },
        "histories": histories,
        "queries": [asdict(q) for q in all_queries],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(__file__).parent / "fixture.json"))
    args = parser.parse_args()

    fixture = build_fixture()
    Path(args.out).write_text(json.dumps(fixture, indent=2, sort_keys=False), encoding="utf-8")
    print(
        f"wrote {args.out}: {fixture['_meta']['history_count']} histories, "
        f"{fixture['_meta']['query_count']} queries "
        f"(dev={fixture['_meta']['dev_query_count']}, "
        f"held_out={fixture['_meta']['held_out_query_count']} "
        f"[answerable={fixture['_meta']['held_out_answerable_count']}, "
        f"absent={fixture['_meta']['held_out_absent_count']}])"
    )


if __name__ == "__main__":
    main()
