"""Value objects shared by the application core and its adapters.

Everything here is plain data: no I/O, no Playwright, no policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LoginStatus(Enum):
    """Outcome of trying to establish an authenticated LinkedIn session."""

    AUTHENTICATED = "authenticated"
    EXPIRED = "expired"
    CHALLENGE = "challenge"


class InviteStatus(Enum):
    """Outcome of one connection-request attempt."""

    REGISTERED = "registered"
    """LinkedIn accepted the click (the Connect button turned into "Pending")."""

    UNREGISTERED = "unregistered"
    """The click happened but the invite did not register (weekly limit)."""

    TIMED_OUT = "timed_out"
    """The click itself timed out; the invite cannot have registered."""

    UNAVAILABLE = "unavailable"
    """No visible Connect button on the card; nothing was clicked, so it can be retried."""

    UNCONFIRMED = "unconfirmed"
    """An unexpected error left the outcome unknown.

    The card counts as attempted (never retried, to avoid a duplicate invite)
    but must not advance the weekly-limit streak, since it says nothing about
    the limit.
    """


class StopReason(Enum):
    """Why a session's scanning loop ended."""

    STALLED = "stalled"
    SCROLL_CAP = "scroll_cap"
    STOPPED = "stopped"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, slots=True)
class Candidate:
    """A profile card reduced to what the matching policy needs."""

    name: str
    text: str


@dataclass(frozen=True, slots=True)
class InviteResult:
    """Result of one attempt, plus a human-readable detail for the log."""

    status: InviteStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PassResult:
    """What one scan-and-click pass over the rendered cards produced."""

    matched: int
    limit_hit: bool


@dataclass(frozen=True, slots=True)
class SessionResult:
    """What one browser session produced, and why it ended."""

    invites: int
    reason: StopReason
