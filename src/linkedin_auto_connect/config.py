"""Tunable settings.

Values are grouped in a frozen dataclass instead of module-level globals so they
can be overridden per run (CLI) and injected in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_NETWORK_URL = "https://www.linkedin.com/mynetwork/grow/"

DEFAULT_KEYWORDS: tuple[str, ...] = (
    "hr",
    "recruiter",
    "talent",
    "acquisition",
    "human",
    "resource",
    "recruitment",
    "recruiting",
    "headhunter",
    "hiring",
    "employment",
    "people",
    "onboarding",
    "learning",
    "looking for",
    "hrbp",
)

DEFAULT_COOKIES_PATH = Path("linkedin_cookies.json")
DEFAULT_IT_CACHE_PATH = Path("linkedin_it_decisions.json")


@dataclass(frozen=True, slots=True)
class Settings:
    """All operational knobs of one run."""

    # --- target ---
    max_invites_per_run: int = 200
    """Stop after this many registered invites; ``0`` disables the cap."""

    keywords: tuple[str, ...] = DEFAULT_KEYWORDS

    # --- matching / scanning ---
    scroll_pixels: int = 600
    scroll_pause_seconds: float = 2.0
    max_passes_per_session: int = 200
    max_no_match_passes: int = 4
    """Passes with rendered cards but no new match before a session counts as stalled."""

    # --- Jev IT-relevance filter ---
    jev_enabled: bool = True
    """Use Jev when it is configured; ``--no-jev`` restores keyword-only matching."""

    jev_timeout_seconds: float = 15.0
    jev_max_attempts: int = 3
    it_yes_threshold: float = 0.7
    it_no_threshold: float = 0.3
    it_evidence_threshold: float = 0.4
    it_cache_path: Path = field(default=DEFAULT_IT_CACHE_PATH)

    # --- invite interaction ---
    click_pause_seconds: float = 1.5
    registration_wait_ms: int = 3000
    max_unregistered_clicks: int = 5
    """Consecutive invites that fail to register before the weekly limit is assumed."""

    # --- browser ---
    headless: bool = True
    slow_mo_ms: int = 0
    """Delay Playwright adds before every action; ``--watch`` raises it for demos."""

    explain_cards: bool = False
    """Log why every rendered card was skipped; ``--watch`` and ``--verbose`` enable it."""

    page_load_timeout_ms: int = 30_000
    max_restarts: int = 15
    restart_base_delay_seconds: float = 5.0
    restart_max_delay_seconds: float = 60.0

    # --- endpoints / storage ---
    login_url: str = LINKEDIN_LOGIN_URL
    network_url: str = LINKEDIN_NETWORK_URL
    cookies_path: Path = field(default=DEFAULT_COOKIES_PATH)

    def restart_delay(self, attempt: int) -> float:
        """Delay before restart number ``attempt`` (1-based), capped."""
        return min(self.restart_base_delay_seconds * attempt, self.restart_max_delay_seconds)
