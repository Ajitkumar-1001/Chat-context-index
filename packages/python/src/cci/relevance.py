"""Local query matching and original-text windows; no model calls or generated evidence."""

from collections import Counter
from collections.abc import Iterator, Sequence
from functools import lru_cache
from unicodedata import category, normalize

_STOP_WORDS = frozenset(
    "a an and are as at be been by can could did do does for from had has have how i in is it "
    "me of on or our please should that the their this to us was we were what when where which "
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


@lru_cache(maxsize=65_536)
def _key(word: str) -> str:
    """A small, deterministic inflection key shared with the TypeScript implementation.

    Rules are generic suffix rules only; `spec/fixtures/term-normalization.json` pins them.
    """
    if len(word) < 4 or not word.isascii() or not word.isalpha():
        return word
    if len(word) == 4:
        if word.endswith("e") or (word.endswith("s") and not word.endswith(("ss", "us", "is"))):
            return word[:-1]
        # ponytail: "used" -> "use" also maps "shed" -> "she"; FTS5 porter stemming if collisions matter.
        if word.endswith("ed") and word[1] not in "aeiou":
            return word[:-1]
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("ied"):
        return word[:-3] + "y"
    doubled = False
    if word.endswith("ing") and len(word) >= 6:
        base = word[:-3]
        doubled = True
    elif word.endswith("ed") and len(word) >= 5:
        base = word[:-2]
        doubled = True
    elif word.endswith("es") and len(word) >= 5:
        base = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        base = word[:-1]
    elif word.endswith("e"):
        base = word[:-1]
    else:
        return word
    if doubled and len(base) > 2 and base[-1] == base[-2] and base[-1] not in "aeiou":
        base = base[:-1]
    return base


def query_terms(query: str) -> list[str]:
    """At most 32 distinct inflection keys from 4,096 query scalars."""
    return list(
        dict.fromkeys(_key(word) for _, _, word in _words(query[:4096]) if word and word not in _STOP_WORDS)
    )[:32]


def fts_term_expression(key: str) -> str:
    """Only literal, bounded inflections are sent to FTS; user operators stay inert."""
    if not key.isascii() or not key.isalpha() or len(key) < 3:
        return '"' + key.replace('"', '""') + '"'
    forms = [key, key + "e", key + "s", key + "es", key + "ed", key + "ing"]
    if key and key[-1] not in "aeiouy":
        forms.extend((key + key[-1] + "ed", key + key[-1] + "ing"))
    if key.endswith("e"):
        forms.append(key + "d")
    if key.endswith("y"):
        forms.extend((key[:-1] + "ies", key[:-1] + "ied"))
    return "(" + " OR ".join(
        '"' + form.replace('"', '""') + '"' for form in dict.fromkeys(forms) if _key(form) == key
    ) + ")"


def relevance(text: str, terms: Sequence[str]) -> int:
    """Distinct term coverage avoids giving repeated old text a frequency advantage."""
    return len(set(terms).intersection(_key(word) for _, _, word in _words(text))) if terms else 0


def excerpt_for_query(text: str, terms: Sequence[str], window: int) -> str:
    """Choose a contiguous original window with most distinct query terms, then the latest tie.

    Offsets count original Unicode scalars, even when normalized matching changes word length.
    A sliding window keeps work linear in the number of matching words.
    """
    if len(text) <= window or not terms:
        return text[:window]
    wanted = set(terms)
    matches = [(start, end, _key(word)) for start, end, word in _words(text) if _key(word) in wanted]
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
