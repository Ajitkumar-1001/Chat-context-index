"""Self-check for generate_fixture.py's output (ponytail: non-trivial logic leaves one runnable
check behind). Verifies: exact split counts (PRD §16.1), and that every answerable query's
acceptable span actually resolves to its claimed correct_value within the claimed message.

Usage: python verify_fixture.py [fixture.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def verify(path: str) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    meta = data["_meta"]

    assert meta["history_count"] == 20, meta["history_count"]
    assert meta["dev_history_count"] == 12
    assert meta["held_out_history_count"] == 8
    assert meta["query_count"] == 100
    assert meta["dev_query_count"] == 60
    assert meta["held_out_query_count"] == 40
    assert meta["held_out_answerable_count"] == 32
    assert meta["held_out_absent_count"] == 8

    histories_by_label = {h["history_label"]: h for h in data["histories"]}
    assert len(histories_by_label) == 20

    for h in data["histories"]:
        assert 300 <= h["message_count"] <= 3000, (h["history_label"], h["message_count"])
        assert len(h["messages"]) == h["message_count"]

    answerable_count = 0
    absent_count = 0
    for q in data["queries"]:
        history = histories_by_label[q["history_label"]]
        if not q["answerable"]:
            absent_count += 1
            assert q["required_evidence_units"] == []
            assert q["answer_rubric"] is None
            continue
        answerable_count += 1
        assert q["required_evidence_units"], q["query_id"]
        for unit in q["required_evidence_units"]:
            msg = history["messages"][unit["message_index"]]
            content = msg["content"]
            assert isinstance(content, str), (q["query_id"], "evidence message must be text")
            span = unit["acceptable_span"]
            excerpt = content[span["start"]:span["end"]]
            assert excerpt == q["answer_rubric"]["correct_value"], (
                q["query_id"], excerpt, q["answer_rubric"]["correct_value"]
            )
            for wrong in q["answer_rubric"]["must_not_cite_values"]:
                assert wrong != excerpt, (q["query_id"], "must_not_cite_values must differ from the correct excerpt")

    print(f"OK: {path}")
    print(f"  20 histories (12 dev / 8 held-out), message counts in range")
    print(f"  {answerable_count} answerable + {absent_count} absent-answer queries")
    print(f"  every answerable query's acceptable span resolves exactly to its rubric's correct_value")


if __name__ == "__main__":
    verify(sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent / "fixture.json"))
