"""Evidence IDs encode retrieval priority; minting and parsing live together (plan-eng-review D9)."""

import pytest
from cci.retrieve import evidence_id, evidence_rank


@pytest.mark.parametrize("rank", [1, 2, 9, 10, 4999])
def test_evidence_id_round_trip(rank: int) -> None:
    assert evidence_rank(evidence_id(rank)) == rank


def test_malformed_evidence_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        evidence_rank("ev_x")
