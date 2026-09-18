import re
from difflib import SequenceMatcher

_WORD = re.compile(r"[a-z0-9]+")


def normalize(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def token_set(text: str) -> set:
    return set(_WORD.findall((text or "").lower()))


def similarity(a: str, b: str) -> float:
    """Blended token-overlap + sequence ratio, 0..1."""
    ta, tb = token_set(a), token_set(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    # cap sequence comparison length for performance on large docs
    # autojunk=False ensures repetitive accounting entries (dates, account numbers) are not ignored
    seq = SequenceMatcher(None, a[:5000], b[:5000], autojunk=False).ratio()
    return round(0.6 * jaccard + 0.4 * seq, 4)
