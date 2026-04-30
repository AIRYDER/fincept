from __future__ import annotations

import re
from collections import Counter


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}


def tokenize(text: str) -> tuple[str, ...]:
    """Tokenize financial headlines/bodies into stable lowercase terms."""
    return tuple(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS and len(token) > 1
    )


def weighted_jaccard(left: str, right: str) -> float:
    """Counter-based Jaccard similarity.

    This keeps repeated event words useful without needing an embedding model.
    It is a deterministic baseline for the later vector-retrieval layer.
    """
    a = Counter(tokenize(left))
    b = Counter(tokenize(right))
    if not a or not b:
        return 0.0
    keys = set(a) | set(b)
    numerator = sum(min(a[k], b[k]) for k in keys)
    denominator = sum(max(a[k], b[k]) for k in keys)
    return numerator / denominator if denominator else 0.0
