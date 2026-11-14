"""Persistent, privacy-friendly cache for Jev audience decisions.

A PYMK page is re-scanned across passes and browser restarts, so the same card
would otherwise be sent to Jev repeatedly. The cache stores only a SHA-256 key
over the normalised card text and the three probabilities; it never stores the
profile text itself.

Mutable lines (``Connect``/``Message`` and mutual-connection hints) are removed
before hashing, so a card stays a cache hit when its social-proof line changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import RelevanceDecision
from .ports import Classifier

logger = logging.getLogger(__name__)

CACHE_INDENT = 2
IGNORED_LINES = {"connect", "message"}


def normalize_card_text(text: str) -> str:
    """Collapse a card to the parts that should influence its decision key."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        lowered = line.lower()
        if lowered in IGNORED_LINES or "mutual connection" in lowered:
            continue
        lines.append(line)
    return " ".join(lines).casefold()


def decision_key(model: str, text: str) -> str:
    """Return the stable cache key for one model and one card text."""
    normalized = normalize_card_text(text)
    return hashlib.sha256(f"{model}\n{normalized}".encode()).hexdigest()


class JsonDecisionCache:
    """Keep Jev probabilities in a small JSON object keyed by card hash."""

    def __init__(self, path: Path, model: str) -> None:
        """Load existing entries, tolerating a missing or corrupt cache."""
        self._path = Path(path)
        self._model = model
        self._entries = self._load()

    @property
    def path(self) -> Path:
        """The file this cache reads from and writes to."""
        return self._path

    def get(self, text: str) -> RelevanceDecision | None:
        """Return a cached decision for *text*, or ``None`` on a miss."""
        entry = self._entries.get(decision_key(self._model, text))
        if not isinstance(entry, dict):
            return None
        try:
            return RelevanceDecision(
                is_hr=float(entry["is_hr"]),
                it_related=float(entry["it_related"]),
                evidence=float(entry["evidence"]),
                model=str(entry.get("model") or self._model),
            )
        except (KeyError, TypeError, ValueError):
            logger.debug("Ignoring a malformed Jev cache entry.", exc_info=True)
            return None

    def put(self, text: str, decision: RelevanceDecision) -> None:
        """Store one decision and persist the cache atomically."""
        self._entries[decision_key(self._model, text)] = {
            "is_hr": decision.is_hr,
            "it_related": decision.it_related,
            "evidence": decision.evidence,
            "model": decision.model or self._model,
            "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._save()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not read the Jev decision cache %s: %s", self._path, exc)
            return {}
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning(
                "Jev decision cache %s is not valid JSON; starting empty.", self._path
            )
            return {}
        if not isinstance(data, dict):
            logger.warning(
                "Jev decision cache %s is not an object; starting empty.", self._path
            )
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                self._entries,
                indent=CACHE_INDENT,
                sort_keys=True,
                ensure_ascii=False,
            )
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                os.replace(temporary_path, self._path)
            except BaseException:
                temporary_path.unlink(missing_ok=True)
                raise
        except OSError as exc:
            # A read-only working directory must not stop the run.
            logger.warning("Could not save the Jev decision cache %s: %s", self._path, exc)


class CachedClassifier:
    """A ``Classifier`` that consults the cache before calling the real model."""

    def __init__(self, classifier: Classifier, cache: JsonDecisionCache) -> None:
        """Wrap *classifier* with *cache*."""
        self._classifier = classifier
        self._cache = cache

    def classify(self, text: str) -> RelevanceDecision:
        """Return a cached decision when possible, otherwise ask and store it."""
        cached = self._cache.get(text)
        if cached is not None:
            label = text.strip().splitlines()[0] if text.strip() else "?"
            logger.debug("[*] Jev cache hit: %.40s", label)
            return cached
        decision = self._classifier.classify(text)
        self._cache.put(text, decision)
        return decision
