"""The development bar's scoring rules (plan-eng-review D18; SC-011 verbatim-copy clarification)."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "dev_recall", Path(__file__).parents[2] / "benchmarks" / "dev_recall.py"
)
assert spec and spec.loader
dev_recall = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev_recall)

MESSAGES = [
    {"content": "Deploy Tuesday."},
    {"content": "Moved: deploy Wednesday."},
    {"content": "Moved: deploy Wednesday."},
]
UNIT = [{"message_index": 1, "acceptable_span": {"start": 14, "end": 23}}]


def test_a_verbatim_copy_counts_only_with_copy_scoring() -> None:
    items = [SimpleNamespace(seq=3, excerpt="Moved: deploy Wednesday.")]
    assert dev_recall._covered(UNIT, MESSAGES, items, copies=True) == 1.0
    assert dev_recall._covered(UNIT, MESSAGES, items, copies=False) == 0.0


def test_bar_floors_stale_only_and_baseline_tolerance() -> None:
    report = {
        "split": "dev",
        "context_recall": 0.86,
        "correction_context_recall": 0.9,
        "stale_only_contexts": [],
    }
    bar = dev_recall._bar(report, {"context_recall": 0.88})
    assert all(check["pass"] for check in bar.values())
    worse = dev_recall._bar({**report, "stale_only_contexts": ["q1"]}, {"context_recall": 0.89})
    assert not worse["stale_only_contexts"]["pass"] and not worse["vs_baseline"]["pass"]
    paraphrase = dev_recall._bar(
        {"split": "paraphrase", "context_recall": 0.5, "bucket_context_recall": {"names_old_value": 0.75}},
        None,
    )
    assert not paraphrase["names_old_value_recall"]["pass"]
