"""In-memory fakes for the application-core tests.

Every fake records the calls it receives so tests can assert ordering and
arguments without touching a browser, the network, or the clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from linkedin_auto_connect.models import (
    Candidate,
    InviteResult,
    InviteStatus,
    LoginStatus,
)
from linkedin_auto_connect.ports import never_stop


class FakeCard:
    """Profile card fake returning scripted invite results."""

    def __init__(
        self,
        name: str,
        text: str = "",
        results: Iterable[InviteResult | InviteStatus] = (),
        *,
        default: InviteResult | InviteStatus = InviteStatus.REGISTERED,
    ) -> None:
        """Build a candidate with the given scripted click outcomes."""
        self.candidate = Candidate(name=name, text=text or name)
        self._results = list(results)
        self._default = default if isinstance(default, InviteResult) else InviteResult(default)
        self.send_calls = 0

    def send_invite(self) -> InviteResult:
        """Return the next scripted result, or the default once exhausted."""
        self.send_calls += 1
        if not self._results:
            return self._default
        result = self._results.pop(0)
        return result if isinstance(result, InviteResult) else InviteResult(result)


class FakeSource:
    """Profile-source fake with scripted wall and card-count signals."""

    def __init__(
        self,
        cards: Sequence[FakeCard] = (),
        *,
        card_counts: Sequence[int] | None = None,
        limit_walls: bool | Sequence[bool] = False,
    ) -> None:
        """Store the rendered cards and the per-call signal scripts."""
        self.cards = list(cards)
        self._card_counts = list(card_counts) if card_counts is not None else None
        self._card_count_last: int | None = None
        self._limit_walls = list(limit_walls) if not isinstance(limit_walls, bool) else None
        self._wall_last = (
            limit_walls
            if isinstance(limit_walls, bool)
            else (bool(limit_walls[-1]) if limit_walls else False)
        )
        self.open_calls = 0
        self.card_count_calls = 0
        self.limit_wall_calls = 0
        self.scroll_calls: list[int] = []

    def open_cards(self) -> Sequence[FakeCard]:
        """Return the same rendered card list on every call."""
        self.open_calls += 1
        return list(self.cards)

    def card_count(self) -> int:
        """Return the next scripted count, or the number of rendered cards."""
        self.card_count_calls += 1
        if self._card_counts:
            self._card_count_last = self._card_counts.pop(0)
            return self._card_count_last
        if self._card_count_last is not None:
            return self._card_count_last
        return len(self.cards)

    def scroll_down(self, pixels: int) -> None:
        """Record one scroll request."""
        self.scroll_calls.append(pixels)

    def limit_wall_visible(self) -> bool:
        """Return the next scripted wall signal, sticky after exhaustion."""
        self.limit_wall_calls += 1
        if self._limit_walls:
            self._wall_last = self._limit_walls.pop(0)
        return self._wall_last


class FakeSession:
    """Browser-session fake recording authentication and close calls."""

    def __init__(
        self,
        source: FakeSource | None = None,
        *,
        statuses: Sequence[LoginStatus] | None = None,
        status: LoginStatus = LoginStatus.AUTHENTICATED,
        cookies: Sequence[dict[str, Any]] = (),
        blocked: bool | Sequence[bool] = False,
        label: str = "session",
        events: list[str] | None = None,
    ) -> None:
        """Build a session with scripted login statuses and blocked signals."""
        self.source = source if source is not None else FakeSource()
        self._statuses = list(statuses) if statuses is not None else [status]
        if not self._statuses:
            self._statuses = [status]
        self._cookies = [dict(cookie) for cookie in cookies]
        self._blocked = list(blocked) if not isinstance(blocked, bool) else None
        self._blocked_last = (
            blocked if isinstance(blocked, bool) else (bool(blocked[-1]) if blocked else False)
        )
        self.label = label
        self._events = events
        self.authenticate_calls: list[dict[str, Any]] = []
        self.blocked_checks = 0
        self.close_calls = 0

    def _record(self, event: str) -> None:
        if self._events is not None:
            self._events.append(f"{self.label}:{event}")

    def authenticate(
        self,
        *,
        allow_manual_login: bool,
        should_stop: Callable[[], bool] = never_stop,
    ) -> LoginStatus:
        """Record the call and return the next scripted login status."""
        self.authenticate_calls.append(
            {"allow_manual_login": allow_manual_login, "should_stop": should_stop}
        )
        self._record("authenticate")
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]

    def cookies(self) -> list[dict[str, Any]]:
        """Return a copy of the scripted cookie jar."""
        self._record("cookies")
        return [dict(cookie) for cookie in self._cookies]

    def is_blocked(self) -> bool:
        """Return the next scripted blocked signal, sticky after exhaustion."""
        self.blocked_checks += 1
        if self._blocked:
            self._blocked_last = self._blocked.pop(0)
        return self._blocked_last

    def close(self) -> None:
        """Record one close call."""
        self.close_calls += 1
        self._record("close")


class FakeSessionFactory:
    """Session factory fake handing out scripted sessions in order."""

    def __init__(
        self,
        sessions: Sequence[FakeSession],
        *,
        events: list[str] | None = None,
    ) -> None:
        """Store the sessions that ``open`` will return, one per call."""
        self._sessions = list(sessions)
        self.headless_flags: list[bool] = []
        self.opened: list[FakeSession] = []
        self._events = events

    def open(self, *, headless: bool) -> FakeSession:
        """Record the requested visibility and return the next session."""
        self.headless_flags.append(headless)
        if self._events is not None:
            self._events.append(f"open:{headless}")
        if not self._sessions:
            raise AssertionError("factory.open called more times than scripted")
        session = self._sessions.pop(0)
        self.opened.append(session)
        return session


class FakeCookieStore:
    """Cookie-store fake recording every saved jar."""

    def __init__(
        self,
        initial: Sequence[dict[str, Any]] | None = None,
        *,
        events: list[str] | None = None,
    ) -> None:
        """Store the cookies that ``load`` should return, if any."""
        self.initial = list(initial) if initial is not None else None
        self.saved: list[list[dict[str, Any]]] = []
        self.load_calls = 0
        self._events = events

    def load(self) -> list[dict[str, Any]] | None:
        """Return the scripted initial cookie jar."""
        self.load_calls += 1
        if self.initial is None:
            return None
        return [dict(cookie) for cookie in self.initial]

    def save(self, cookies: Sequence[dict[str, Any]]) -> None:
        """Record one saved jar, refusing an empty one like the real store."""
        if not cookies:
            return
        self.saved.append([dict(cookie) for cookie in cookies])
        if self._events is not None:
            self._events.append("store:save")


class FakeLimitPolicy:
    """Limit-policy fake tracking the per-run cap and failure streak."""

    def __init__(
        self,
        *,
        cap: int = 0,
        sent: int = 0,
        unregistered_limit: int = 5,
    ) -> None:
        """Configure the cap, the starting count, and the failure threshold."""
        self.cap = cap
        self._sent = sent
        self.unregistered_limit = unregistered_limit
        self.unregistered_streak = 0
        self.registered_calls = 0
        self.unregistered_calls = 0

    @property
    def sent(self) -> int:
        """Return registered invites so far in this run."""
        return self._sent

    @property
    def at_invite_cap(self) -> bool:
        """Return True when a positive per-run cap has been reached."""
        return self.cap > 0 and self._sent >= self.cap

    def record_registered(self) -> None:
        """Count one registered invite and reset the failure streak."""
        self._sent += 1
        self.registered_calls += 1
        self.unregistered_streak = 0

    def record_unregistered(self) -> bool:
        """Count one failed invite and report whether the limit is assumed."""
        self.unregistered_calls += 1
        self.unregistered_streak += 1
        return self.unregistered_streak >= self.unregistered_limit
