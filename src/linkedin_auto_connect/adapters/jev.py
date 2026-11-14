"""Jev adapter: ask a System One model whether a card is HR and IT-related.

Jev (TypeSafe AI, served by the OpenCode Console) answers typed ``noul``
questions with probabilities instead of generating text. The adapter is an
edge module: it talks HTTP, reads no Playwright state, and is configured from
two environment variables:

``JEV_API_KEY``
    The OpenCode Console (or TypeSafe) API key.
``JEV_BASE_URL``
    The System One base URL, for example
    ``https://opencode.ai/inference/systemone``.

The rest of the package depends on
:class:`linkedin_auto_connect.domain.ports.Classifier`, never on this module.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain.matcher import candidate_name
from ..domain.models import JevRuntimeError, JevSetupError, RelevanceDecision

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://opencode.ai/inference/systemone"
DEFAULT_MODEL = "jev-1.13-free"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 1.0
ENDPOINT_SUFFIX = "/v1/systemone"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
SETUP_ERROR_CODES = frozenset({400, 401, 402, 404})
HTTP_FORBIDDEN = 403
HTTP_TOO_MANY_REQUESTS = 429
HTTP_OVERLOADED = 529
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 600

Poster = Callable[[str, Mapping[str, str], bytes, float], dict[str, Any]]

QUESTIONS: dict[str, dict[str, object]] = {
    "is_hr_role": {
        "type": "noul",
        "instructions": {
            "question": "Does `candidate.name` work in HR or recruiting?",
            "focus": (
                "Count HR, talent acquisition, recruiting, people operations, "
                "headhunting, and closely related people roles. Do not count a "
                "business manager who hires (a hiring manager) or anyone in a "
                "non-HR function whose headline merely contains an HR keyword."
            ),
        },
        "criteria": {
            "true": {
                "what": (
                    "Works in HR, recruiting, talent acquisition, people "
                    "operations, or headhunting"
                ),
                "examples": [
                    "IT Recruiter",
                    "Talent Acquisition Specialist",
                    "HR Business Partner",
                    "People Operations Lead",
                    "Headhunter",
                ],
            },
            "false": {
                "what": (
                    "Works in a non-HR function, even if the headline contains "
                    "an HR keyword"
                ),
                "examples": [
                    "Software Engineer",
                    "Hiring Manager (a business manager who hires, not HR)",
                    "Machine Learning Engineer",
                    "M&A Analyst",
                    "Customer Onboarding Specialist",
                    "Employment Lawyer",
                ],
            },
        },
    },
    "it_role_or_company": {
        "type": "noul",
        "instructions": {
            "question": (
                "Is `candidate.name`'s current role or employer part of the IT industry?"
            ),
            "focus": (
                "Count both: (a) the employer is an IT, software, product, or IT "
                "outsourcing company; and (b) the person recruits for IT roles even "
                "at a non-IT employer. Do not count generic HR work at a non-IT company."
            ),
        },
        "criteria": {
            "true": {
                "what": "Employer is an IT company, or the role is IT recruiting",
                "examples": [
                    "IT Recruiter at an IT recruitment agency",
                    "Technical Recruiter at a bank",
                    "HR Business Partner at a software company",
                ],
            },
            "false": {
                "what": (
                    "Generic HR or recruiting at a non-IT employer, with no IT "
                    "hiring focus"
                ),
                "examples": [
                    "HR Generalist at a pharmaceutical company",
                    "Recruiting Intern at an oil and gas group",
                    "HR Business Partner at a retailer",
                ],
            },
        },
    },
    "has_enough_evidence": {
        "type": "noul",
        "instructions": {
            "question": (
                "Does `candidate.card_text` contain enough information about the "
                "employer or the role to judge whether it is IT-related?"
            ),
            "focus": (
                "A headline with a role and an employer, or an explicit IT role, is "
                "enough. A name alone, or a generic HR title with no employer, is not."
            ),
        },
    },
}


@dataclass(frozen=True, slots=True)
class JevConfig:
    """Connection and model settings for one run."""

    api_key: str = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` file, ignoring comments and blank lines."""
    values: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read env file %s: %s", path, exc)
        return values
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if name:
            values[name] = value.strip().strip('"').strip("'")
    return values


def find_env_file() -> Path | None:
    """Prefer ``.env`` in the working directory, then the project root."""
    candidates = (
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_jev_config(
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    env_file: Path | None = None,
) -> JevConfig | None:
    """Return a config when both Jev variables are present, otherwise ``None``.

    Environment variables win over the file, so CI and one-off overrides do not
    need to edit ``.env``.
    """
    path = Path(env_file) if env_file is not None else find_env_file()
    values = load_env_file(path) if path is not None and path.is_file() else {}
    api_key = (os.environ.get("JEV_API_KEY") or values.get("JEV_API_KEY") or "").strip()
    base_url = (
        os.environ.get("JEV_BASE_URL") or values.get("JEV_BASE_URL") or ""
    ).strip()
    if not api_key or not base_url:
        return None
    if not base_url.startswith(("http://", "https://")):
        logger.warning("JEV_BASE_URL must start with http:// or https://; Jev is disabled.")
        return None
    return JevConfig(
        api_key=api_key,
        base_url=base_url,
        model=DEFAULT_MODEL,
        timeout_seconds=timeout_seconds,
        max_attempts=max(1, max_attempts),
    )


def systemone_endpoint(base_url: str) -> str:
    """Return the full System One endpoint for a base or already-full URL."""
    url = base_url.strip().rstrip("/")
    if url.endswith(ENDPOINT_SUFFIX):
        return url
    return url + ENDPOINT_SUFFIX


def _post_json(
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> dict[str, Any]:
    """POST a JSON body and map HTTP failures onto the package's error types."""
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        message = f"Jev HTTP {exc.code}: {detail[:300]}"
        if exc.code in SETUP_ERROR_CODES:
            raise JevSetupError(message) from exc
        if exc.code == HTTP_FORBIDDEN:
            # Cloudflare may reject a request by signature even with a valid key.
            raise JevRuntimeError(message) from exc
        if (
            exc.code in (HTTP_TOO_MANY_REQUESTS, HTTP_OVERLOADED)
            or HTTP_SERVER_ERROR_MIN <= exc.code < HTTP_SERVER_ERROR_MAX
        ):
            raise JevRuntimeError(message) from exc
        raise JevSetupError(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevRuntimeError(f"Jev request failed: {exc}") from exc
    except ValueError as exc:
        raise JevRuntimeError(f"Jev returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise JevRuntimeError("Jev returned a non-object response.")
    return payload


def _noul(answers: Mapping[str, Any], name: str) -> float:
    """Read one Noul probability, rejecting malformed or out-of-range answers."""
    answer = answers.get(name)
    if not isinstance(answer, Mapping) or "noul" not in answer:
        raise JevRuntimeError(f"Jev response is missing the {name!r} Noul answer.")
    try:
        value = float(answer["noul"])
    except (TypeError, ValueError) as exc:
        raise JevRuntimeError(f"Jev returned a non-numeric {name!r} answer.") from exc
    if not 0.0 <= value <= 1.0:
        raise JevRuntimeError(f"Jev returned an out-of-range {name!r} answer: {value}.")
    return value


class JevClassifier:
    """Ask Jev three independent questions about one profile card."""

    def __init__(
        self,
        config: JevConfig,
        *,
        poster: Poster | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Store the connection settings and the injectable transport."""
        self._config = config
        self._poster = poster or _post_json
        self._sleeper = sleeper

    def classify(self, text: str) -> RelevanceDecision:
        """Return the HR, IT and evidence probabilities for *text*.

        Raises :class:`JevSetupError` when the configuration or credentials are
        rejected (fallback to keyword matching is safe), and
        :class:`JevRuntimeError` for transient failures (the caller should skip
        the card rather than connecting blindly).
        """
        card_text = text or ""
        payload = {
            "model": self._config.model,
            "state": {
                "candidate": {
                    "name": candidate_name(card_text),
                    "card_text": card_text,
                }
            },
            "questions": QUESTIONS,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        response = self._request(body)
        answers = response.get("answers")
        if not isinstance(answers, Mapping):
            raise JevRuntimeError("Jev response is missing the answers object.")
        return RelevanceDecision(
            is_hr=_noul(answers, "is_hr_role"),
            it_related=_noul(answers, "it_role_or_company"),
            evidence=_noul(answers, "has_enough_evidence"),
            model=str(response.get("model") or self._config.model),
        )

    def _request(self, body: bytes) -> dict[str, Any]:
        url = systemone_endpoint(self._config.base_url)
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        attempts = max(1, self._config.max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                return self._poster(url, headers, body, self._config.timeout_seconds)
            except JevRuntimeError:
                if attempt >= attempts:
                    raise
                self._sleeper(self._config.retry_backoff_seconds * attempt)
        raise JevRuntimeError("Jev request failed after all attempts.")
