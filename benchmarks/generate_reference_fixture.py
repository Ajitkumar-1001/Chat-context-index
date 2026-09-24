"""Export the Python reference benchmark's exact synthetic messages and queries for TypeScript."""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from reference_fixture_benchmark import (
    _WORDS,
    BATCH_SIZE,
    SEED,
    TOTAL_MESSAGES,
    _synthetic_content,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--queries", type=int, default=1000)
    parser.add_argument("--messages", type=int, default=TOTAL_MESSAGES)
    args = parser.parse_args()
    if args.messages <= 0 or args.messages % BATCH_SIZE:
        parser.error(f"--messages must be a positive multiple of {BATCH_SIZE}")
    rng = random.Random(SEED)

    def batch() -> list[dict[str, str]]:
        return [
            {"role": "user" if i % 2 == 0 else "assistant", "content": _synthetic_content(rng)}
            for i in range(BATCH_SIZE)
        ]

    messages = [message for _ in range(args.messages // BATCH_SIZE) for message in batch()]
    queries = [" ".join(rng.sample(_WORDS, 2)) for _ in range(args.queries)]
    concurrent_batches = {
        str(level): [batch() for _ in range(32)] for level in (4, 16)
    }
    args.out.write_text(json.dumps({
        "seed": SEED,
        "batch_size": BATCH_SIZE,
        "messages": messages,
        "queries": queries,
        "concurrent_batches": concurrent_batches,
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
