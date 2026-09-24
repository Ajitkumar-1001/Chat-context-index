"""Shared term-normalization contract: Python must match every row the TypeScript suite also checks."""

import json
from pathlib import Path

import pytest
from cci.relevance import fts_term_expression, query_terms

CASES = json.loads(
    (Path(__file__).parents[2] / "spec/fixtures/term-normalization.json").read_text(encoding="utf-8")
)["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["word"])
def test_term_normalization_matches_shared_fixture(case: dict) -> None:
    terms = query_terms(case["word"])
    assert terms == case["query_terms"]
    assert (fts_term_expression(terms[0]) if terms else None) == case["fts_expression"]
