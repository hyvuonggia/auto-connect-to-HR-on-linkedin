"""Keyword matching policy for candidate profile cards.

The policy is deliberately tiny and pure: it turns a list of keywords into
precompiled word-boundary regexes once, then tests card text against them. No
I/O, no logging and no browser access -- so it can be unit-tested directly.
"""

from __future__ import annotations

import re
from collections.abc import Sequence


class KeywordMatcher:
    """Match text against any of a fixed set of keywords.

    Keywords are normalised to lowercase, stripped, deduplicated and compiled
    into case-insensitive word-boundary patterns. Word boundaries keep short
    keywords such as ``"an"`` from matching inside words like ``"Leanne"``,
    while multi-word keywords such as ``"looking for"`` still match as a phrase.
    """

    def __init__(self, keywords: Sequence[str]) -> None:
        """Normalise *keywords* and precompile one pattern per distinct keyword."""
        normalised: list[str] = []
        seen: set[str] = set()
        for raw in keywords:
            keyword = raw.strip().lower()
            if not keyword or keyword in seen:
                continue
            seen.add(keyword)
            normalised.append(keyword)
        self._keywords = tuple(normalised)
        self._patterns: tuple[re.Pattern[str], ...] = tuple(
            re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE) for keyword in normalised
        )

    @property
    def keywords(self) -> tuple[str, ...]:
        """The normalised keywords, in first-seen order."""
        return self._keywords

    def matches(self, text: str | None) -> bool:
        """Return True when *text* contains one of the keywords as a whole word."""
        if not text:
            return False
        return any(pattern.search(text) for pattern in self._patterns)
