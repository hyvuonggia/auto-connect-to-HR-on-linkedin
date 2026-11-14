"""Audience matching policies for candidate profile cards.

``KeywordMatcher`` is the deliberately tiny, pure prefilter. ``ItRelevanceMatcher``
composes it with a Jev classifier: a card must first contain an HR keyword, then
Jev must confirm that the person is HR and that their HR work is tied to the IT
industry. Both policies are unit-testable without a browser or a network.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from .models import JevRuntimeError, JevSetupError, RelevanceDecision
from .ports import Classifier, Matcher

logger = logging.getLogger(__name__)

HR_THRESHOLD = 0.7
"""Minimum probability that the person actually works in HR or recruiting."""

MAX_CONSECUTIVE_FAILURES = 5
"""Jev runtime failures before the composite matcher stops calling it."""


def candidate_name(text: str | None) -> str:
    """Return the first line of a card, with a connection-degree marker removed."""
    if not text:
        return "?"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("•"):
            continue
        name = stripped.split(" • ")[0].strip()
        return name or "?"
    return "?"


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


class ItRelevanceMatcher:
    """Require the keyword prefilter, then Jev's HR and IT-relevance decisions.

    The two gates are deliberate: the keyword list is broad (``people``,
    ``learning``, ``hiring`` also appear in non-HR headlines), so Jev first
    confirms the person is in HR/recruiting, then asks whether that HR work is
    tied to the IT industry. A card is connected only when both probabilities
    and the evidence probability pass their thresholds.

    Failure policy:

    * A setup rejection (bad key, no credit, malformed request) is treated as
      "Jev is not configured": the matcher logs once and falls back to the
      keyword decision, preserving the old HR-only behaviour.
    * A runtime failure (timeout, rate limit, server error) skips the card, and
      five consecutive failures disable Jev for the rest of the run so a dead
      endpoint cannot silently turn into non-IT invites.
    """

    def __init__(
        self,
        inner: Matcher,
        classifier: Classifier,
        *,
        yes_threshold: float = 0.7,
        no_threshold: float = 0.3,
        evidence_threshold: float = 0.4,
        hr_threshold: float = HR_THRESHOLD,
        max_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        """Store the prefilter, the Jev classifier, and the decision thresholds."""
        self._inner = inner
        self._classifier = classifier
        self._yes_threshold = yes_threshold
        self._no_threshold = no_threshold
        self._evidence_threshold = evidence_threshold
        self._hr_threshold = hr_threshold
        self._max_failures = max(1, max_failures)
        self._consecutive_failures = 0
        self._setup_fallback = False
        self._unavailable = False

    @property
    def jev_active(self) -> bool:
        """False once setup fell back or the runtime failure budget is spent."""
        return not (self._setup_fallback or self._unavailable)

    def matches(self, text: str | None) -> bool:
        """Return True only for an HR keyword match that Jev accepts."""
        if not self._inner.matches(text):
            return False
        if self._setup_fallback:
            return True
        if self._unavailable:
            return False

        try:
            decision = self._classifier.classify(text or "")
        except JevSetupError as exc:
            self._setup_fallback = True
            logger.warning(
                "[!] Jev setup failed (%s) — falling back to keyword-only HR matching.",
                exc,
            )
            return True
        except JevRuntimeError as exc:
            self._consecutive_failures += 1
            logger.warning(
                "  [!] Jev failed (%d/%d) for %.40s: %s — skipping.",
                self._consecutive_failures,
                self._max_failures,
                candidate_name(text),
                exc,
            )
            if self._consecutive_failures >= self._max_failures:
                self._unavailable = True
                logger.error(
                    "[!] Jev unavailable after %d consecutive failures — skipping "
                    "the remaining cards.",
                    self._max_failures,
                )
            return False

        self._consecutive_failures = 0
        return self._decide(decision, candidate_name(text))

    def _decide(self, decision: RelevanceDecision, name: str) -> bool:
        """Apply the HR, evidence, and IT thresholds and log the reason."""
        values = (
            decision.is_hr,
            decision.it_related,
            decision.evidence,
        )
        if decision.is_hr < self._hr_threshold:
            self._log(values, "skip (not HR)", name)
            return False
        if decision.evidence < self._evidence_threshold:
            self._log(values, "skip (not enough evidence)", name)
            return False
        if decision.it_related >= self._yes_threshold:
            self._log(values, "connect", name)
            return True
        reason = "not IT" if decision.it_related <= self._no_threshold else "uncertain IT"
        self._log(values, f"skip ({reason})", name)
        return False

    @staticmethod
    def _log(values: tuple[float, float, float], verdict: str, name: str) -> None:
        logger.info(
            "  [Jev] HR=%.2f IT=%.2f evidence=%.2f -> %s: %.40s",
            values[0],
            values[1],
            values[2],
            verdict,
            name,
        )
