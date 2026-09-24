"""Local query matching and original-text windows; no model calls or generated evidence."""

from collections import Counter
from collections.abc import Iterator, Sequence
from unicodedata import category, normalize

_STOP_WORDS = frozenset(
    "a an and are as at be been by can could did do does for from had has have how i in is it "
    "me of on or our please should that the their this to us was were what when where which "
    "who why will with would you your".split()
)


def _words(text: str) -> Iterator[tuple[int, int, str]]:
    start: int | None = None
    for end, char in enumerate(text + " "):
        if char.isalnum() or category(char).startswith("M"):
            if start is None:
                start = end
        elif start is not None:
            word = "".join(
                c for c in normalize("NFD", text[start:end]).lower() if not category(c).startswith("M")
            )
            yield start, end, word
            start = None


def query_terms(query: str) -> list[str]:
    """At most 32 distinct content terms from 4,096 query scalars; operators stay literal."""
    return list(
        dict.fromkeys(word for _, _, word in _words(query[:4096]) if word and word not in _STOP_WORDS)
    )[:32]


def relevance(text: str, terms: Sequence[str]) -> int:
    """Distinct term coverage avoids giving repeated old text a frequency advantage."""
    return len(set(terms).intersection(word for _, _, word in _words(text))) if terms else 0


def excerpt_for_query(text: str, terms: Sequence[str], window: int) -> str:
    """Choose a contiguous original window with most distinct query terms, then the latest tie.

    Offsets count original Unicode scalars, even when normalized matching changes word length.
    A sliding window keeps work linear in the number of matching words.
    """
    if len(text) <= window or not terms:
        return text[:window]
    wanted = set(terms)
    matches = [(start, end, word) for start, end, word in _words(text) if word in wanted]
    counts: Counter[str] = Counter()
    left = 0
    best_score = best_start = best_end = 0
    for right, (_, end, word) in enumerate(matches):
        counts[word] += 1
        while left <= right and end - matches[left][0] > window:
            removed = matches[left][2]
            counts[removed] -= 1
            if not counts[removed]:
                del counts[removed]
            left += 1
        if counts and len(counts) >= best_score:
            best_score, best_start, best_end = len(counts), matches[left][0], end
    start = max(0, best_end - window, best_start - window // 4)
    start = min(start, max(0, len(text) - window))
    return text[start : start + window]
