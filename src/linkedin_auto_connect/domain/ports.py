"""Ports (interfaces) the application core depends on.

The domain and application modules import only from this module and
:mod:`linkedin_auto_connect.domain.models`. They never import Playwright: the
browser adapter implements these protocols, and the tests implement them with
fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from .models import Candidate, InviteResult, LoginStatus, RelevanceDecision


def never_stop() -> bool:
    """Default ``should_stop`` for callers that never want to abort."""
    return False


class ProfileCard(Protocol):
    """One rendered "People you may know" card.

    A card is a live handle on the page, not a snapshot: ``send_invite`` must
    re-locate the Connect button on every call.
    """

    @property
    def candidate(self) -> Candidate:
        """The card data the matching policy needs (name and visible text)."""

    def send_invite(self) -> InviteResult:
        """Click Connect and verify that the invite registered.

        Must never raise for an expected browser condition (missing or
        intercepted button, click timeout); report it through ``InviteStatus``.
        """


class ProfileSource(Protocol):
    """The page the runner scans for candidate cards."""

    def open_cards(self) -> Sequence[ProfileCard]:
        """Snapshot the cards currently rendered, in page order."""

    def card_count(self) -> int:
        """Number of rendered cards; ``0`` means the page is still loading."""

    def scroll_down(self, pixels: int) -> None:
        """Scroll the page to load more cards."""

    def limit_wall_visible(self) -> bool:
        """True when LinkedIn shows a visible weekly-invitation-limit message.

        At the limit LinkedIn may still accept clicks and show "Pending" while
        dropping invites server-side, so this signal decides when to stop even
        in headless mode.
        """


class BrowserSession(Protocol):
    """One open browser, ready to authenticate and then be scanned."""

    @property
    def source(self) -> ProfileSource:
        """The network page of this session."""

    def authenticate(
        self,
        *,
        allow_manual_login: bool,
        should_stop: Callable[[], bool] = never_stop,
    ) -> LoginStatus:
        """Load saved cookies and verify them.

        When the cookies are stale but the session is allowed to log in by hand
        (a visible browser), wait for the user and persist nothing here -- the
        caller owns persistence. ``should_stop`` lets a long manual login abort
        on Ctrl+C.
        """

    def cookies(self) -> list[dict[str, Any]]:
        """Current cookie jar, ready to be handed to a ``CookieStore``."""

    def is_blocked(self) -> bool:
        """True when LinkedIn is showing a challenge or login wall."""

    def close(self) -> None:
        """Release the browser. Must be safe to call more than once."""


class SessionFactory(Protocol):
    """Creates browser sessions of a given visibility."""

    def open(self, *, headless: bool) -> BrowserSession: ...


class CookieStore(Protocol):
    """Persistence for the authenticated cookie jar."""

    def load(self) -> list[dict[str, Any]] | None:
        """Return saved cookies, or ``None`` when there are none to use."""

    def save(self, cookies: Sequence[dict[str, Any]]) -> None: ...


class Matcher(Protocol):
    """Decides whether a card belongs to the target audience."""

    def matches(self, text: str | None) -> bool: ...


class Classifier(Protocol):
    """Answers the narrower audience questions for one card.

    Implemented by the Jev adapter and by the caching wrapper; the composite
    matcher depends only on this protocol, so it can be tested with a fake.
    """

    def classify(self, text: str) -> RelevanceDecision: ...


class LimitPolicy(Protocol):
    """Tracks how close a run is to the LinkedIn weekly invitation limit."""

    @property
    def sent(self) -> int:
        """Registered invites so far in this run."""

    @property
    def unregistered_streak(self) -> int:
        """Consecutive invites that failed to register; reset by a success."""

    @property
    def at_invite_cap(self) -> bool:
        """True when the configured per-run invite cap has been reached."""

    def record_registered(self) -> None:
        """Count one invite LinkedIn accepted; resets the failure streak."""

    def record_unregistered(self) -> bool:
        """Count one invite that failed to register.

        Returns True when the streak is long enough to assume the weekly limit.
        """
