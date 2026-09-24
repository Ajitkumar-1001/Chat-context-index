"""A capacity diagnostic must never become an unbounded or held-out run."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "unit_provider_capacity", ROOT / "evaluations/check_provider_capacity.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def plan():
    return SimpleNamespace(
        kind="smoke", split="development", trials=1,
        limits=SimpleNamespace(
            max_calls=10, max_reserved_tokens=100_000, max_estimated_usd=0.04,
            concurrency=1, minimum_request_interval_s=5,
        ),
    )


def test_bounded_smoke_is_allowed():
    MODULE.validate_capacity_plan(plan())


@pytest.mark.parametrize("kind,split,trials", [("native", "held_out", 3), ("cache", "development", 1)])
def test_diagnostic_rejects_non_smoke(kind, split, trials):
    value = plan()
    value.kind, value.split, value.trials = kind, split, trials
    with pytest.raises(ValueError, match="requires_development_smoke"):
        MODULE.validate_capacity_plan(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_calls", 11),
        ("max_reserved_tokens", 100_001),
        ("max_estimated_usd", 0.05),
        ("concurrency", 2),
        ("minimum_request_interval_s", 0),
    ],
)
def test_diagnostic_cannot_widen_capacity_envelope(field, value):
    data = plan()
    setattr(data.limits, field, value)
    with pytest.raises(ValueError, match="exceeds_smoke_envelope"):
        MODULE.validate_capacity_plan(data)
